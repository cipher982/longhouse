//! Durable command receipts and the completed-command cache that make
//! command delivery idempotent across reconnects and restarts.

use super::*;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub(super) struct CommandReceiptIdentity {
    pub(super) command_id: String,
    pub(super) provider: String,
    pub(super) session_id: String,
    pub(super) generation: Option<String>,
    pub(super) run_id: Option<String>,
    pub(super) connection_id: Option<String>,
    pub(super) request_hash: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(super) struct DurableCommandReceipt {
    pub(super) identity: CommandReceiptIdentity,
    pub(super) state: String,
    pub(super) result: Option<Value>,
    pub(super) terminal_at_ms: Option<i64>,
}

pub(super) enum DurableCommandReceiptOutcome {
    Claimed,
    Accepted,
    Terminal(Value),
    IdentityConflict,
}

pub(super) struct DurableCommandReceiptStore {
    pub(super) root: PathBuf,
    pub(super) lock: Mutex<()>,
}

impl DurableCommandReceiptStore {
    pub(super) fn for_db_path(db_path: &Path) -> Self {
        let root = db_path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join(COMMAND_RECEIPT_DIR);
        Self {
            root,
            lock: Mutex::new(()),
        }
    }

    pub(super) fn unavailable(&self) -> Result<()> {
        // A transient filesystem failure is rechecked on the next request,
        // rather than poisoning control until the whole engine is restarted.
        std::fs::create_dir_all(&self.root).with_context(|| {
            format!(
                "creating durable command receipt directory {}",
                self.root.display()
            )
        })
    }

    pub(super) fn path_for(&self, command_id: &str) -> PathBuf {
        self.root
            .join(format!("{:x}.json", Sha256::digest(command_id.as_bytes())))
    }

    pub(super) fn read_unlocked(&self, command_id: &str) -> Result<Option<DurableCommandReceipt>> {
        let path = self.path_for(command_id);
        let contents = match std::fs::read_to_string(&path) {
            Ok(contents) => contents,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error).with_context(|| format!("reading {}", path.display())),
        };
        let receipt = serde_json::from_str(&contents)
            .with_context(|| format!("parsing managed-control receipt {}", path.display()))?;
        Ok(Some(receipt))
    }

    pub(super) fn outcome_for(
        &self,
        receipt: DurableCommandReceipt,
        identity: &CommandReceiptIdentity,
    ) -> DurableCommandReceiptOutcome {
        if receipt.identity != *identity {
            return DurableCommandReceiptOutcome::IdentityConflict;
        }
        match receipt.state.as_str() {
            "accepted" => DurableCommandReceiptOutcome::Accepted,
            "terminal" => receipt
                .result
                .map(DurableCommandReceiptOutcome::Terminal)
                .unwrap_or(DurableCommandReceiptOutcome::Accepted),
            _ => DurableCommandReceiptOutcome::Accepted,
        }
    }

    pub(super) fn claim(
        &self,
        identity: &CommandReceiptIdentity,
    ) -> Result<DurableCommandReceiptOutcome> {
        self.unavailable()?;
        let _guard = self.lock.lock().expect("command receipt lock poisoned");
        if let Some(receipt) = self.read_unlocked(&identity.command_id)? {
            return Ok(self.outcome_for(receipt, identity));
        }

        let receipt = DurableCommandReceipt {
            identity: identity.clone(),
            state: "accepted".to_string(),
            result: None,
            terminal_at_ms: None,
        };
        let bytes = serde_json::to_vec(&receipt).context("serializing managed-control receipt")?;
        let path = self.path_for(&identity.command_id);
        let mut file = match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                return Ok(match self.read_unlocked(&identity.command_id)? {
                    Some(existing) => self.outcome_for(existing, identity),
                    None => bail!("managed-control receipt disappeared while claiming it"),
                })
            }
            Err(error) => {
                return Err(error).with_context(|| format!("creating {}", path.display()))
            }
        };
        if let Err(error) = file.write_all(&bytes).and_then(|_| file.sync_all()) {
            let _ = std::fs::remove_file(&path);
            return Err(error).with_context(|| format!("writing {}", path.display()));
        }
        // The filename itself must survive a crash before any provider action.
        std::fs::File::open(&self.root)?.sync_all()?;
        Ok(DurableCommandReceiptOutcome::Claimed)
    }

    pub(super) fn complete(
        &self,
        identity: &CommandReceiptIdentity,
        result: &Value,
    ) -> Result<Option<Value>> {
        self.unavailable()?;
        let bytes = serde_json::to_vec(result).context("serializing managed-control result")?;
        if bytes.len() > COMMAND_RECEIPT_RESULT_MAX_BYTES {
            bail!(
                "managed-control result is {} bytes; receipt limit is {} bytes",
                bytes.len(),
                COMMAND_RECEIPT_RESULT_MAX_BYTES
            );
        }
        let _guard = self.lock.lock().expect("command receipt lock poisoned");
        let Some(existing) = self.read_unlocked(&identity.command_id)? else {
            bail!("managed-control receipt disappeared before result recording");
        };
        if existing.identity != *identity {
            bail!("managed-control receipt identity changed before result recording");
        }
        if existing.state == "terminal" {
            return Ok(existing.result);
        }

        let path = self.path_for(&identity.command_id);
        let temp_path = path.with_extension(format!("json.tmp-{}", std::process::id()));
        let receipt = DurableCommandReceipt {
            identity: identity.clone(),
            state: "terminal".to_string(),
            result: Some(result.clone()),
            terminal_at_ms: Some(timestamp_now_ms()),
        };
        let encoded = serde_json::to_vec(&receipt).context("serializing terminal receipt")?;
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp_path)
            .with_context(|| format!("creating {}", temp_path.display()))?;
        if let Err(error) = file.write_all(&encoded).and_then(|_| file.sync_all()) {
            let _ = std::fs::remove_file(&temp_path);
            return Err(error).with_context(|| format!("writing {}", temp_path.display()));
        }
        std::fs::rename(&temp_path, &path)
            .with_context(|| format!("installing {}", path.display()))?;
        std::fs::File::open(&self.root)?.sync_all()?;
        Ok(None)
    }
}

pub(super) fn timestamp_now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

pub(super) fn command_requires_restart_fence(frame: &Value) -> bool {
    matches!(
        frame.get("command_type").and_then(Value::as_str),
        Some(
            COMMAND_SEND_TEXT
                | COMMAND_INTERRUPT
                | COMMAND_STEER_TEXT
                | COMMAND_ANSWER_PAUSE
                | COMMAND_TERMINATE
                | COMMAND_TURN_INTERRUPT
                | COMMAND_INVOCATION_CLOSE
                | COMMAND_TURN_STEER
        )
    )
}

pub(super) fn command_receipt_identity(frame: &Value, command_id: &str) -> CommandReceiptIdentity {
    let mut payload = frame.get("payload").cloned().unwrap_or_else(|| json!({}));
    let provider = payload
        .get("provider")
        .and_then(Value::as_str)
        .unwrap_or(DEFAULT_COMMAND_PROVIDER)
        .trim()
        .to_string();
    let grant = payload
        .as_object_mut()
        .and_then(|object| object.remove("longhouse_control_grant"));
    let scope = |key: &str| {
        grant
            .as_ref()
            .and_then(|grant| grant.get(key))
            .filter(|value| !value.is_null())
            .map(|value| {
                value
                    .as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| value.to_string())
            })
    };
    let request =
        serde_json::to_vec(&(frame.get("command_type"), &payload)).expect("JSON values serialize");
    CommandReceiptIdentity {
        command_id: command_id.to_string(),
        provider,
        session_id: frame
            .get("session_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim()
            .to_string(),
        generation: scope("lease_generation"),
        run_id: scope("run_id"),
        connection_id: scope("connection_id"),
        request_hash: format!("{:x}", Sha256::digest(&request)),
    }
}

pub(super) struct CachedCommandResult {
    pub(super) completed_at: Instant,
    pub(super) result: Value,
}

pub(super) struct CompletedCommandCache {
    pub(super) capacity: usize,
    pub(super) ttl: Duration,
    pub(super) entries: HashMap<String, CachedCommandResult>,
    pub(super) order: VecDeque<String>,
    pub(super) durable_receipts: Option<Arc<DurableCommandReceiptStore>>,
}

impl CompletedCommandCache {
    pub(super) fn new(capacity: usize, ttl: Duration) -> Self {
        Self {
            capacity,
            ttl,
            entries: HashMap::new(),
            order: VecDeque::new(),
            durable_receipts: None,
        }
    }

    pub(super) fn with_durable_receipts(
        mut self,
        store: Option<Arc<DurableCommandReceiptStore>>,
    ) -> Self {
        self.durable_receipts = store;
        self
    }

    pub(super) fn get(&mut self, command_id: &str) -> Option<Value> {
        self.prune(Instant::now());
        self.entries
            .get(command_id)
            .map(|cached| cached.result.clone())
    }

    pub(super) fn insert(&mut self, command_id: String, result: Value) {
        if self.capacity == 0 {
            return;
        }
        let now = Instant::now();
        self.prune(now);
        if !self.entries.contains_key(&command_id) {
            self.order.push_back(command_id.clone());
        }
        self.entries.insert(
            command_id,
            CachedCommandResult {
                completed_at: now,
                result,
            },
        );
        while self.entries.len() > self.capacity {
            let Some(oldest) = self.order.pop_front() else {
                break;
            };
            self.entries.remove(&oldest);
        }
    }

    pub(super) fn prune(&mut self, now: Instant) {
        while let Some(command_id) = self.order.front() {
            let expired = self
                .entries
                .get(command_id)
                .map(|cached| now.duration_since(cached.completed_at) >= self.ttl)
                .unwrap_or(true);
            if !expired {
                break;
            }
            let Some(command_id) = self.order.pop_front() else {
                break;
            };
            self.entries.remove(&command_id);
        }
    }
}
