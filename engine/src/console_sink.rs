//! Envelope plumbing shared by the Console (headless, UI-driven) adapters.
//!
//! `claude_print`, `cursor_print`, `omp_print`, `pi_print`,
//! `antigravity_print` and `opencode_run` each carried a sink with the same
//! run identity and hand-built the same runtime-event envelope, status-slot
//! phase, local phase record and terminal handoff, differing only in the
//! provider id, adapter id, dedupe prefix and execution lifetime. Those four
//! values live in a [`ConsoleProvider`]; everything a run's records share
//! lives in a [`ConsoleRun`]. Payload contents that genuinely differ stay in
//! the adapters.

use std::path::PathBuf;

use chrono::{DateTime, Utc};
use serde_json::{json, Value};

/// What tells one Console adapter's records apart from another's.
#[derive(Debug)]
pub struct ConsoleProvider {
    /// Provider id on the wire, in the runtime key and the status slot.
    pub provider: &'static str,
    /// Adapter id: the event `source`, `managed_transport` and `terminal_source`.
    pub adapter: &'static str,
    /// Dedupe-key prefix, also the stderr log tag (`claude-print`).
    pub tag: &'static str,
    /// The `execution_lifetime` the adapter reports.
    pub lifetime: &'static str,
}

/// The identity every record of one Console run carries.
#[derive(Clone, Debug)]
pub struct ConsoleRun {
    pub provider: &'static ConsoleProvider,
    pub session_id: String,
    pub thread_id: String,
    pub turn_id: Option<String>,
    pub run_id: String,
    pub client_request_id: Option<String>,
    pub launch_id: String,
    pub process_group_id: Option<i32>,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
    pub runtime_events_outbox_dir: PathBuf,
}

impl ConsoleRun {
    pub fn runtime_key(&self) -> String {
        format!("{}:{}", self.provider.provider, self.session_id)
    }

    /// One runtime event in the envelope ingest expects.
    pub fn event(
        &self,
        source: &str,
        kind: &str,
        occurred_at: String,
        dedupe_key: String,
        payload: Value,
    ) -> Value {
        json!({
            "runtime_key": self.runtime_key(),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": self.provider.provider,
            "device_id": self.machine_name,
            "source": source,
            "kind": kind,
            "occurred_at": occurred_at,
            "dedupe_key": dedupe_key,
            "payload": payload,
        })
    }

    /// An adapter event deduplicated within this run:
    /// `{tag}:{session}:{run}:{suffix}`.
    pub fn run_event(&self, kind: &str, suffix: &str, payload: Value) -> Value {
        self.event(
            self.provider.adapter,
            kind,
            Utc::now().to_rfc3339(),
            format!(
                "{}:{}:{}:{suffix}",
                self.provider.tag, self.session_id, self.run_id
            ),
            payload,
        )
    }

    /// `payload` plus this adapter's `managed_transport` and `execution_lifetime`.
    pub fn with_transport(&self, mut payload: Value) -> Value {
        payload["managed_transport"] = json!(self.provider.adapter);
        payload["execution_lifetime"] = json!(self.provider.lifetime);
        payload
    }

    /// The provider-identity binding, deduplicated per launch rather than per
    /// run: one invocation binds once however many turns it serves.
    pub fn binding_event(&self, payload: Value) -> Value {
        self.event(
            self.provider.adapter,
            "binding_signal",
            Utc::now().to_rfc3339(),
            format!(
                "{}:{}:{}:binding",
                self.provider.tag, self.session_id, self.launch_id
            ),
            self.with_transport(payload),
        )
    }

    /// A delegation snapshot, deduplicated by its own observation time.
    pub fn delegation_event(&self, source: &str, dedupe_tag: &str, snapshot: Value) -> Value {
        let observed_at = snapshot
            .get("observed_at")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| Utc::now().to_rfc3339());
        let dedupe_key = format!(
            "{dedupe_tag}:{}:{}:delegation:{}",
            self.launch_id, self.run_id, snapshot["observed_at"]
        );
        self.event(
            source,
            "delegation_signal",
            observed_at,
            dedupe_key,
            json!({"delegation": snapshot}),
        )
    }

    /// A wake request. It belongs to the invocation, not a run, so it carries
    /// no `run_id`.
    pub fn wake_event(&self, source: &str, wake: &crate::console_lifecycle::WakeRequest) -> Value {
        json!({
            "runtime_key": self.runtime_key(),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "provider": self.provider.provider,
            "device_id": self.machine_name,
            "source": source,
            "kind": "wake_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("wake:{}", wake.wake_id),
            "payload": {
                "invocation_id": wake.invocation_id,
                "wake_id": wake.wake_id,
                "provider_thread_id": wake.provider_thread_id,
                "trigger": wake.trigger
            }
        })
    }

    pub fn post_event(&self, event: &Value) {
        if let Err(error) =
            crate::outbox::enqueue_runtime_event(&self.runtime_events_outbox_dir, event)
        {
            eprintln!(
                "[{}] runtime outbox write failed: {error}",
                self.provider.tag
            );
        }
    }

    /// Publish a phase to this session's status slot. The daemon records the
    /// local ledger from it and sends it; only records no later event can
    /// restate (binding, terminal) stay on the durable queue. `extra` adds
    /// adapter-specific keys to the slot payload.
    pub fn publish_phase(
        &self,
        phase: &str,
        tool_name: Option<&str>,
        extra: Option<(&str, Value)>,
    ) {
        let mut payload = json!({
            "execution_lifetime": self.provider.lifetime,
            "thread_id": self.thread_id,
            "device_id": self.machine_name,
        });
        if let Some((key, value)) = extra {
            payload[key] = value;
        }
        crate::status_slot::publish_console_phase(
            self.provider.provider,
            self.provider.adapter,
            &self.session_id,
            &self.run_id,
            &Utc::now().to_rfc3339(),
            phase,
            tool_name,
            payload,
        );
    }

    pub fn persist_local_phase(
        &self,
        phase: &str,
        tool_name: Option<&str>,
        observed_at: DateTime<Utc>,
    ) {
        let Some(db_path) = self.local_db_path.as_deref() else {
            return;
        };
        if let Err(err) = crate::hook_outbox::enqueue_local_phase(
            db_path,
            &self.session_id,
            self.provider.provider,
            phase,
            tool_name,
            self.provider.adapter,
            &observed_at.to_rfc3339(),
            Some(self.run_id.as_str()),
        ) {
            eprintln!(
                "[{}] enqueue local phase failed for {}: {err}",
                self.provider.tag, self.session_id
            );
        }
    }

    /// The fields every adapter's terminal payload shares. Adapters add their
    /// own identity fields (`provider_thread_id`, `source_path`).
    pub fn terminal_payload(
        &self,
        terminal_state: &str,
        terminal_reason: &str,
        exit_code: Option<i32>,
        stderr_tail: Option<&str>,
    ) -> Value {
        self.with_transport(json!({
            "terminal_state": terminal_state,
            "terminal_reason": terminal_reason,
            "terminal_source": self.provider.adapter,
            "exit_code": exit_code,
            "stderr_tail": stderr_tail,
            "turn_id": self.turn_id,
            "client_request_id": self.client_request_id,
        }))
    }

    /// Attach the invocation lifecycle a persistent adapter reports with its
    /// terminal, when it has one.
    pub fn with_invocation(
        &self,
        mut payload: Value,
        invocation_state: Option<&str>,
        pending_count: Option<usize>,
    ) -> Value {
        if let (Some(state), Some(count)) = (invocation_state, pending_count) {
            payload["invocation"] = json!({
                "id": self.launch_id,
                "state": state,
                "pending_count": count
            });
        }
        payload
    }

    /// Retain the terminal on the turn claim and hand it to the runtime
    /// outbox. The status slot is retired only once the outbox owns the
    /// record; if the claim cannot be written the event goes straight to the
    /// outbox so it is not lost.
    pub fn hand_off_terminal(
        &self,
        terminal_state: &str,
        terminal_error: Option<String>,
        terminal_event: Value,
    ) {
        let tag = self.provider.tag;
        let handoff = crate::turn_claims::default_registry().and_then(|registry| {
            crate::outbox::retain_and_enqueue_terminal_event(
                &registry,
                &self.runtime_events_outbox_dir,
                &self.run_id,
                terminal_state,
                terminal_error,
                terminal_event.clone(),
            )
        });
        match handoff {
            Ok((_, true)) => crate::status_slot::retire_console_run(
                self.provider.provider,
                self.provider.adapter,
                &self.session_id,
                &self.run_id,
            ),
            Ok((_, false)) => eprintln!(
                "[{tag}] terminal record remains pending for {} run {}; keeping the status slot",
                self.session_id, self.run_id
            ),
            Err(error) => {
                eprintln!(
                    "[{tag}] terminal claim write failed for {} run {}: {error:#}; keeping the status slot",
                    self.session_id, self.run_id
                );
                self.post_event(&terminal_event);
            }
        }
    }

    /// The same run identity rebound to another turn of the invocation.
    pub fn for_binding(&self, binding: &crate::console_lifecycle::TurnBinding) -> Self {
        let mut run = self.clone();
        run.run_id = binding.run_id.clone();
        run.turn_id = binding.turn_id.clone();
        run.client_request_id = binding.client_request_id.clone();
        run
    }
}

/// Put the spawned provider in a process group of its own, so teardown can
/// signal the whole tree. `after` runs in the child after `setpgid`.
#[cfg(unix)]
pub fn own_process_group<F>(command: &mut tokio::process::Command, mut after: F)
where
    F: FnMut() -> std::io::Result<()> + Send + Sync + 'static,
{
    // SAFETY: the closure runs between fork and exec and only calls
    // async-signal-safe functions (setpgid, and whatever `after` does, which
    // every caller keeps to dup2/open on a pre-built CString).
    unsafe {
        command.pre_exec(move || {
            if libc::setpgid(0, 0) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            after()
        });
    }
}

/// Golden capture of everything a Console sink writes: runtime-outbox events,
/// local phase records, status slots and the turn claim. The fixtures under
/// `tests/fixtures/console_envelopes/` were captured before the adapters
/// shared this module, so they pin each adapter's wire output exactly.
/// `LONGHOUSE_UPDATE_GOLDEN=1` rewrites them.
#[cfg(test)]
pub(crate) mod golden {
    use std::path::{Path, PathBuf};

    use serde_json::{json, Value};

    pub(crate) const SESSION: &str = "00000000-0000-4000-8000-0000000000ee";
    pub(crate) const THREAD: &str = "00000000-0000-4000-8000-0000000000bb";
    pub(crate) const TURN: &str = "golden-turn";
    pub(crate) const RUN: &str = "00000000-0000-4000-8000-0000000000aa";
    pub(crate) const CLIENT_REQUEST: &str = "golden-client-request";
    pub(crate) const LAUNCH: &str = "golden-launch";
    pub(crate) const MACHINE: &str = "golden-machine";
    pub(crate) const PROVIDER_THREAD: &str = "golden-provider-thread";

    /// A scratch Longhouse home with a turn claim for `RUN`.
    pub(crate) struct GoldenHome {
        pub(crate) temp: tempfile::TempDir,
    }

    impl GoldenHome {
        pub(crate) fn new(provider: &str) -> Self {
            let temp = tempfile::tempdir().unwrap();
            let home = Self { temp };
            crate::turn_claims::TurnClaimRegistry::new(home.agent_dir().join("turn-claims"))
                .claim(RUN, SESSION, THREAD, None, None, provider)
                .unwrap();
            home
        }

        pub(crate) fn agent_dir(&self) -> PathBuf {
            self.temp.path().join("agent")
        }

        pub(crate) fn outbox(&self) -> PathBuf {
            self.agent_dir().join("runtime-events-outbox")
        }

        pub(crate) fn local_db(&self) -> PathBuf {
            self.agent_dir().join("longhouse.db")
        }

        /// Run `body` with this home as `LONGHOUSE_HOME`, then capture.
        pub(crate) fn run<F>(&self, body: F) -> Value
        where
            F: std::future::Future<Output = ()>,
        {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            let home = self.temp.path().as_os_str();
            temp_env::with_vars(
                [("LONGHOUSE_HOME", Some(home)), ("HOME", Some(home))],
                || {
                    runtime.block_on(body);
                    self.capture()
                },
            )
        }

        fn capture(&self) -> Value {
            let home = self.temp.path().to_string_lossy().to_string();
            let mut outbox = read_json_files(&self.outbox(), "");
            let mut local_phases = read_json_files(&self.agent_dir().join("outbox"), "prs.");
            let mut status = self.status_rows();
            let claim =
                crate::turn_claims::TurnClaimRegistry::new(self.agent_dir().join("turn-claims"))
                    .read(RUN)
                    .ok()
                    .map(|claim| {
                        json!({
                            "state": claim.state,
                            "terminal_event": claim.terminal_event,
                            "terminal_event_handed_off": claim.terminal_event_handed_off,
                            "invocation_state": claim.invocation_state,
                            "pending_count": claim.pending_count,
                        })
                    });
            for list in [&mut outbox, &mut local_phases, &mut status] {
                list.sort_by_key(|value| value.to_string());
            }
            let mut captured = json!({
                "outbox": outbox,
                "local_phases": local_phases,
                "status": status,
                "claim": claim,
            });
            normalize(&mut captured, &home);
            captured
        }
    }

    impl GoldenHome {
        /// The live status slots, for a capture before a terminal retires them.
        pub(crate) fn status_rows(&self) -> Vec<Value> {
            let mut rows: Vec<Value> = crate::status_slot::read_all(
                &crate::status_slot::status_slot_dir(&self.agent_dir()),
            )
            .into_iter()
            .map(|slot| {
                json!({
                    "session_id": slot.session_id,
                    "provider": slot.provider,
                    "runtime_key": slot.runtime_key,
                    "run_id": slot.run_id,
                    "source": slot.source,
                    "phase": slot.phase,
                    "tool_name": slot.tool_name,
                    "payload": slot.payload,
                })
            })
            .collect();
            let home = self.temp.path().to_string_lossy().to_string();
            rows.iter_mut().for_each(|row| normalize(row, &home));
            rows.sort_by_key(|value| value.to_string());
            rows
        }
    }

    fn read_json_files(dir: &Path, prefix: &str) -> Vec<Value> {
        let Ok(entries) = std::fs::read_dir(dir) else {
            return Vec::new();
        };
        entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                let name = path.file_name().unwrap().to_string_lossy();
                name.starts_with(prefix) && name.ends_with(".json")
            })
            .map(|path| serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap())
            .collect()
    }

    /// Blank clocks and the scratch home so captures compare across runs.
    fn normalize(value: &mut Value, home: &str) {
        match value {
            Value::Object(map) => {
                for (key, value) in map.iter_mut() {
                    if matches!(
                        key.as_str(),
                        "occurred_at" | "observed_at" | "observed_at_ms"
                    ) && !value.is_null()
                    {
                        *value = json!("<clock>");
                    } else {
                        normalize(value, home);
                    }
                }
            }
            Value::Array(items) => items.iter_mut().for_each(|item| normalize(item, home)),
            Value::String(text) if text.contains(home) => {
                *text = text.replace(home, "<home>");
            }
            _ => {}
        }
    }

    pub(crate) fn assert_golden(name: &str, actual: &Value) {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/console_envelopes")
            .join(format!("{name}.json"));
        if std::env::var_os("LONGHOUSE_UPDATE_GOLDEN").is_some() {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            let mut text = serde_json::to_string_pretty(actual).unwrap();
            text.push('\n');
            std::fs::write(&path, text).unwrap();
            return;
        }
        let expected: Value = serde_json::from_slice(
            &std::fs::read(&path).unwrap_or_else(|_| panic!("missing golden {}", path.display())),
        )
        .unwrap();
        pretty_assertions::assert_eq!(&expected, actual, "golden {name} drifted");
    }
}
