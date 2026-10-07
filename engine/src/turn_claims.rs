use std::collections::HashMap;
use std::fs;
use std::fs::OpenOptions;
use std::io::ErrorKind;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, Mutex};

/// In-process monitor ownership is separate from the durable claim. When a
/// duplicate command proves the provider process is gone, it must also stop
/// the old monitor so that its conversation lock is released before retry.
static ACTIVE_MONITORS: LazyLock<Mutex<HashMap<String, Arc<AtomicBool>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

pub struct MonitorLease {
    run_id: String,
    cancel: Arc<AtomicBool>,
}

impl Drop for MonitorLease {
    fn drop(&mut self) {
        if let Ok(mut monitors) = ACTIVE_MONITORS.lock() {
            if monitors
                .get(&self.run_id)
                .is_some_and(|current| Arc::ptr_eq(current, &self.cancel))
            {
                monitors.remove(&self.run_id);
            }
        }
    }
}

pub fn register_monitor(run_id: &str) -> MonitorLease {
    let cancel = Arc::new(AtomicBool::new(false));
    if let Ok(mut monitors) = ACTIVE_MONITORS.lock() {
        monitors.insert(run_id.to_string(), cancel.clone());
    }
    MonitorLease {
        run_id: run_id.to_string(),
        cancel,
    }
}

pub fn cancel_monitor(run_id: &str) -> bool {
    ACTIVE_MONITORS
        .lock()
        .ok()
        .and_then(|monitors| monitors.get(run_id).cloned())
        .map(|cancel| {
            cancel.store(true, Ordering::Release);
            true
        })
        .unwrap_or(false)
}

pub fn monitor_is_active(run_id: &str) -> bool {
    ACTIVE_MONITORS
        .lock()
        .ok()
        .is_some_and(|monitors| monitors.contains_key(run_id))
}

#[cfg(test)]
pub fn monitor_cancel_requested(run_id: &str) -> bool {
    ACTIVE_MONITORS
        .lock()
        .ok()
        .and_then(|monitors| monitors.get(run_id).cloned())
        .is_some_and(|cancel| cancel.load(Ordering::Acquire))
}

use anyhow::Context;
use anyhow::Result;
use chrono::Utc;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use uuid::Uuid;

const CLAIM_SCHEMA_VERSION: u32 = 7;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct OwnedProcessIdentity {
    pub pid: u32,
    pub process_group_id: i32,
    pub process_start_time: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TurnClaim {
    pub schema_version: u32,
    pub run_id: String,
    pub session_id: String,
    pub thread_id: String,
    #[serde(default)]
    pub turn_id: Option<String>,
    #[serde(default)]
    pub client_request_id: Option<String>,
    #[serde(default)]
    pub provider_thread_id: Option<String>,
    #[serde(default)]
    pub provider_identity_confirmed: bool,
    #[serde(default)]
    pub source_path: Option<String>,
    pub provider: String,
    pub state: String,
    pub claimed_at: String,
    pub updated_at: String,
    pub pid: Option<u32>,
    #[serde(default)]
    pub process_group_id: Option<i32>,
    /// Boot this claim's pid and process group were recorded under. Absent on
    /// claims written before this field existed, which is treated as unknown.
    #[serde(default)]
    pub boot_id: Option<String>,
    pub process_start_time: Option<String>,
    #[serde(default)]
    pub adapter: Option<String>,
    #[serde(default)]
    pub launch_id: Option<String>,
    #[serde(default)]
    pub invocation_state: Option<String>,
    #[serde(default)]
    pub pending_count: usize,
    #[serde(default)]
    pub origin: Option<String>,
    #[serde(default)]
    pub adopted_parked_invocation: bool,
    #[serde(default)]
    pub stdout_path: Option<String>,
    #[serde(default)]
    pub stderr_path: Option<String>,
    #[serde(default)]
    pub cancel_requested_at: Option<String>,
    #[serde(default)]
    pub projected_stdout_offset: u64,
    #[serde(default)]
    pub projected_seq: u64,
    /// Process identities observed while the invocation was live. Recovery
    /// needs these when the direct leader has already exited and reparented a
    /// descendant out of the original process tree.
    #[serde(default)]
    pub owned_processes: Vec<OwnedProcessIdentity>,
    pub result: Option<Value>,
    pub error: Option<String>,
    /// Exact terminal runtime event retained until durable outbox handoff.
    #[serde(default)]
    pub terminal_event: Option<Value>,
    /// True only after this exact event has been durably handed to the outbox.
    #[serde(default)]
    pub terminal_event_handed_off: bool,
    /// Closing a parked invocation is independent of its completed response.
    #[serde(default)]
    pub invocation_close_event: Option<Value>,
    #[serde(default)]
    pub invocation_close_event_handed_off: bool,
}

impl TurnClaim {
    pub fn has_pending_runtime_handoff(&self) -> bool {
        (self.terminal_event.is_some() && !self.terminal_event_handed_off)
            || (self.invocation_close_event.is_some() && !self.invocation_close_event_handed_off)
    }

    /// Whether this claim's recorded process group id can still be trusted to
    /// name the group this claim spawned.
    ///
    /// A pid is never reallocated while it is still in use as a process group
    /// id, so within one boot the recorded id either names our own group or
    /// names nothing, and signalling it is safe. A reboot removes that
    /// guarantee: claims outlive reboots because nothing prunes them, every pid
    /// is free again, and the same number can name a completely unrelated
    /// group. Recovery reaches the signalling path exactly when the recorded
    /// process is gone, so it has no other way to tell those apart.
    ///
    /// Claims written before this field existed report false, which costs at
    /// most some orphaned children and never costs someone else's processes.
    pub fn process_group_is_from_this_boot(&self) -> bool {
        match (
            self.boot_id.as_deref(),
            crate::heartbeat::machine_boot_id().as_deref(),
        ) {
            (Some(recorded), Some(current)) => recorded == current,
            _ => false,
        }
    }
}

#[derive(Debug)]
pub enum ClaimOutcome {
    Acquired,
    Existing(TurnClaim),
}

#[derive(Clone, Debug)]
pub struct TurnClaimRegistry {
    root: PathBuf,
}

impl TurnClaimRegistry {
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    pub fn claim(
        &self,
        run_id: &str,
        session_id: &str,
        thread_id: &str,
        turn_id: Option<&str>,
        client_request_id: Option<&str>,
        provider: &str,
    ) -> Result<ClaimOutcome> {
        validate_id(run_id, "run_id")?;
        validate_id(session_id, "session_id")?;
        validate_id(thread_id, "thread_id")?;
        let _lock = self.lock_run(run_id)?;
        let path = self.claim_path(run_id);
        let now = Utc::now().to_rfc3339();
        let claim = TurnClaim {
            schema_version: CLAIM_SCHEMA_VERSION,
            run_id: run_id.to_string(),
            session_id: session_id.to_string(),
            thread_id: thread_id.to_string(),
            turn_id: turn_id.map(str::to_string),
            client_request_id: client_request_id.map(str::to_string),
            provider_thread_id: None,
            provider_identity_confirmed: false,
            source_path: None,
            provider: provider.to_string(),
            state: "claimed".to_string(),
            // Recorded at spawn, alongside the pid and process group it
            // describes; a claim with no process yet has no boot to record.
            boot_id: None,
            claimed_at: now.clone(),
            updated_at: now,
            pid: None,
            process_group_id: None,
            process_start_time: None,
            adapter: None,
            launch_id: None,
            invocation_state: None,
            pending_count: 0,
            origin: None,
            adopted_parked_invocation: false,
            stdout_path: None,
            stderr_path: None,
            cancel_requested_at: None,
            projected_stdout_offset: 0,
            projected_seq: 0,
            owned_processes: Vec::new(),
            result: None,
            error: None,
            terminal_event: None,
            terminal_event_handed_off: false,
            invocation_close_event: None,
            invocation_close_event_handed_off: false,
        };
        let bytes = serde_json::to_vec_pretty(&claim)?;
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                set_private_file_permissions(&file)?;
                file.write_all(&bytes)?;
                file.sync_all()?;
                drop(file);
                crate::outbox::sync_directory(&self.root)?;
                Ok(ClaimOutcome::Acquired)
            }
            Err(err) if err.kind() == ErrorKind::AlreadyExists => {
                Ok(ClaimOutcome::Existing(self.read(run_id)?))
            }
            Err(err) => Err(err).with_context(|| format!("creating turn claim {}", path.display())),
        }
    }
    /// Publish OMP's launch-scoped directory hold before the provider can
    /// create its first native file. Exact identity binding replaces this
    /// directory hint with the transcript path.
    pub fn set_pending_omp_session_dir(
        &self,
        run_id: &str,
        session_dir: &Path,
    ) -> Result<TurnClaim> {
        anyhow::ensure!(
            session_dir.is_absolute(),
            "OMP pending session directory must be absolute"
        );
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        anyhow::ensure!(
            claim.provider.eq_ignore_ascii_case("omp") && claim.state == "claimed",
            "OMP session directory can only be reserved on a claimed OMP turn"
        );
        let mut result = claim.result.take().unwrap_or_else(|| serde_json::json!({}));
        result
            .as_object_mut()
            .context("OMP pending turn claim result is not an object")?
            .insert("session_dir".into(), serde_json::json!(session_dir));
        claim.result = Some(result);
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn mark_spawned(
        &self,
        run_id: &str,
        pid: Option<u32>,
        process_group_id: Option<i32>,
        process_start_time: Option<String>,
        adapter: &str,
        result: Value,
    ) -> Result<TurnClaim> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if claim.state == "terminal" || claim.state == "failed" || claim.terminal_event.is_some() {
            return Ok(claim);
        }
        claim.state = "spawned".to_string();
        claim.pid = pid;
        claim.process_group_id = process_group_id;
        claim.boot_id = crate::heartbeat::machine_boot_id();
        claim.process_start_time = process_start_time;
        claim.owned_processes = pid
            .zip(process_group_id)
            .map(|(pid, process_group_id)| {
                vec![OwnedProcessIdentity {
                    pid,
                    process_group_id,
                    process_start_time: claim.process_start_time.clone(),
                }]
            })
            .unwrap_or_default();
        claim.adapter = Some(adapter.to_string());
        claim.result = Some(result);
        claim.error = None;
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn mark_spawned_invocation(
        &self,
        run_id: &str,
        pid: u32,
        process_group_id: i32,
        process_start_time: Option<String>,
        adapter: &str,
        launch_id: &str,
        provider_thread_id: Option<&str>,
        stdout_path: &str,
        stderr_path: &str,
        result: Value,
    ) -> Result<TurnClaim> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if claim.state == "terminal" || claim.state == "failed" || claim.terminal_event.is_some() {
            return Ok(claim);
        }
        claim.state = "spawned".to_string();
        claim.pid = Some(pid);
        claim.process_group_id = Some(process_group_id);
        claim.boot_id = crate::heartbeat::machine_boot_id();
        claim.process_start_time = process_start_time;
        claim.owned_processes = vec![OwnedProcessIdentity {
            pid,
            process_group_id,
            process_start_time: claim.process_start_time.clone(),
        }];
        claim.adapter = Some(adapter.to_string());
        claim.launch_id = Some(launch_id.to_string());
        claim.provider_thread_id = provider_thread_id.map(str::to_string);
        claim.stdout_path = Some(stdout_path.to_string());
        claim.stderr_path = Some(stderr_path.to_string());
        claim.result = Some(result);
        claim.error = None;
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }
    pub fn record_invocation_turn(
        &self,
        run_id: &str,
        origin: &str,
        adopted_parked_invocation: bool,
    ) -> Result<TurnClaim> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if claim.state == "terminal" || claim.state == "failed" || claim.terminal_event.is_some() {
            return Ok(claim);
        }
        claim.origin = Some(origin.to_string());
        claim.adopted_parked_invocation = adopted_parked_invocation;
        claim.invocation_state = Some("responding".to_string());
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn record_invocation_state(
        &self,
        run_id: &str,
        invocation_state: &str,
        pending_count: usize,
    ) -> Result<TurnClaim> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if claim.invocation_state.as_deref() == Some("closed") {
            return Ok(claim);
        }
        claim.invocation_state = Some(invocation_state.to_string());
        claim.pending_count = pending_count;
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn mark_cancel_requested(&self, run_id: &str) -> Result<TurnClaim> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if claim.state != "spawned" {
            anyhow::bail!("turn claim {run_id} is not an active invocation");
        }
        claim.cancel_requested_at = Some(Utc::now().to_rfc3339());
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn mark_projection_checkpoint(
        &self,
        run_id: &str,
        stdout_offset: u64,
        seq: u64,
    ) -> Result<TurnClaim> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if stdout_offset < claim.projected_stdout_offset || seq < claim.projected_seq {
            anyhow::bail!("turn claim {run_id} projection checkpoint cannot move backwards");
        }
        claim.projected_stdout_offset = stdout_offset;
        claim.projected_seq = seq;
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn record_owned_processes(
        &self,
        run_id: &str,
        observed: Vec<OwnedProcessIdentity>,
    ) -> Result<TurnClaim> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if claim.state != "spawned" {
            return Ok(claim);
        }
        let mut merged = claim.owned_processes;
        for identity in observed {
            if identity.pid == 0 || identity.process_group_id <= 0 {
                continue;
            }
            if let Some(existing) = merged.iter_mut().find(|item| item.pid == identity.pid) {
                *existing = identity;
            } else {
                merged.push(identity);
            }
        }
        merged.sort_by_key(|identity| identity.pid);
        merged.dedup_by_key(|identity| identity.pid);
        merged.truncate(256);
        claim.owned_processes = merged;
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn list_nonterminal(&self) -> Result<Vec<TurnClaim>> {
        Ok(self
            .list_all()?
            .into_iter()
            .filter(|claim| claim.state != "terminal" && claim.state != "failed")
            .collect())
    }

    pub fn list_all(&self) -> Result<Vec<TurnClaim>> {
        Ok(self.list_all_shared()?.to_vec())
    }

    /// The same claims as `list_all`, shared. A scan asks this once per source it
    /// examines, so the directory is read again only when a claim file changed.
    pub fn list_all_shared(&self) -> Result<Arc<Vec<TurnClaim>>> {
        crate::dir_cache::parsed_json_dir(&self.root, parse_claim_file, |left, right| {
            left.claimed_at.cmp(&right.claimed_at)
        })
        .context("reading turn claim registry")
    }

    /// Preserve an OMP-reported path while its lazy native source is pending.
    /// Only the later provider-authored header can confirm archive identity.
    pub fn set_pending_omp_source(
        &self,
        run_id: &str,
        provider_thread_id: &str,
        source_path: &Path,
    ) -> Result<TurnClaim> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        anyhow::ensure!(
            claim.provider == "omp" && claim.state == "spawned",
            "only a spawned OMP invocation can reserve its reported source"
        );
        let session_dir = claim
            .result
            .as_ref()
            .and_then(|result| result.get("session_dir"))
            .and_then(Value::as_str)
            .context("OMP invocation has no pending session directory")?;
        anyhow::ensure!(
            !provider_thread_id.trim().is_empty()
                && crate::omp_session::session_file_path_in_session_dir(
                    Path::new(session_dir),
                    source_path
                ),
            "OMP reported source is outside its exact invocation scope"
        );
        claim.provider_thread_id = Some(provider_thread_id.to_string());
        claim.provider_identity_confirmed = false;
        claim.source_path = Some(source_path.to_string_lossy().into_owned());
        if let Some(result) = claim.result.as_mut().and_then(Value::as_object_mut) {
            result.insert("session_file".into(), serde_json::json!(source_path));
        }
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn mark_provider_binding(
        &self,
        run_id: &str,
        provider_thread_id: &str,
        source_path: Option<&str>,
    ) -> Result<TurnClaim> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        claim.provider_thread_id = Some(provider_thread_id.to_string());
        claim.provider_identity_confirmed = true;
        claim.source_path = source_path.map(str::to_string);
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn mark_failed(&self, run_id: &str, error: &str) -> Result<TurnClaim> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if claim.state == "terminal" || claim.terminal_event.is_some() {
            return Ok(claim);
        }
        claim.state = "failed".to_string();
        claim.error = Some(error.to_string());
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn mark_terminal(
        &self,
        run_id: &str,
        terminal_state: &str,
        error: Option<String>,
    ) -> Result<TurnClaim> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if claim.state == "terminal" || claim.state == "failed" {
            return Ok(claim);
        }
        claim.state = "terminal".to_string();
        claim.error = error;
        claim.updated_at = Utc::now().to_rfc3339();
        if let Some(result) = claim.result.as_mut().and_then(Value::as_object_mut) {
            result.insert(
                "terminal_state".to_string(),
                Value::String(terminal_state.to_string()),
            );
        } else {
            claim.result = Some(serde_json::json!({"terminal_state": terminal_state}));
        }
        self.write(&claim)?;
        Ok(claim)
    }

    /// Atomically record this run's terminal fact and its exact runtime event.
    /// A previously retained event is immutable; retries always hand off that
    /// original payload, including its original timestamp and dedupe key.
    /// Returns the exact pending event to enqueue, or `None` after its handoff.
    /// A thin terminal fact cannot veto the first exact event.
    pub fn mark_terminal_with_event(
        &self,
        run_id: &str,
        terminal_state: &str,
        error: Option<String>,
        event: Value,
    ) -> Result<Option<Value>> {
        anyhow::ensure!(
            event.get("kind").and_then(Value::as_str) == Some("terminal_signal"),
            "retained event for run {run_id} is not a terminal signal"
        );
        anyhow::ensure!(
            event.get("run_id").and_then(Value::as_str) == Some(run_id),
            "terminal event run_id does not match claim {run_id}"
        );
        anyhow::ensure!(
            event
                .pointer("/payload/terminal_state")
                .and_then(Value::as_str)
                == Some(terminal_state),
            "terminal event state does not match claim {run_id}"
        );

        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if let Some(retained) = claim.terminal_event.as_ref() {
            anyhow::ensure!(
                retained
                    .pointer("/payload/terminal_state")
                    .and_then(Value::as_str)
                    == Some(terminal_state),
                "conflicting exact terminal event for run {run_id}"
            );
            return Ok(if claim.terminal_event_handed_off {
                None
            } else {
                claim.terminal_event
            });
        }
        claim.state = "terminal".to_string();
        claim.error = error;
        claim.updated_at = Utc::now().to_rfc3339();
        if let Some(result) = claim.result.as_mut().and_then(Value::as_object_mut) {
            result.insert(
                "terminal_state".to_string(),
                Value::String(terminal_state.to_string()),
            );
        } else {
            claim.result = Some(serde_json::json!({"terminal_state": terminal_state}));
        }
        if let Some(invocation) = event
            .pointer("/payload/invocation")
            .filter(|_| claim.invocation_state.as_deref() != Some("closed"))
        {
            if let Some(state) = invocation.get("state").and_then(Value::as_str) {
                claim.invocation_state = Some(state.to_string());
            }
            if let Some(pending_count) = invocation
                .get("pending_count")
                .and_then(Value::as_u64)
                .and_then(|count| usize::try_from(count).ok())
            {
                claim.pending_count = pending_count;
            }
        }
        claim.terminal_event = Some(event);
        self.write(&claim)?;
        Ok(claim.terminal_event)
    }

    /// Exact event still needing a durable outbox handoff, if any.
    pub fn pending_terminal_event(&self, run_id: &str) -> Result<Option<Value>> {
        let claim = self.read(run_id)?;
        Ok(if claim.terminal_event_handed_off {
            None
        } else {
            claim.terminal_event
        })
    }

    /// Acknowledge only the exact retained event after its durable handoff.
    /// False means the event no longer matches this run and must not retire it.
    pub fn mark_terminal_event_handed_off(&self, run_id: &str, event: &Value) -> Result<bool> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if claim.terminal_event.as_ref() != Some(event)
            || (claim.state != "terminal" && claim.state != "failed")
        {
            return Ok(false);
        }
        if claim.terminal_event_handed_off {
            return Ok(true);
        }
        claim.terminal_event_handed_off = true;
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(true)
    }

    /// Whether this claim's exact terminal event has been handed to the durable
    /// outbox and is therefore safe for its status owner to retire.
    pub fn terminal_event_handed_off(&self, run_id: &str) -> Result<bool> {
        let claim = self.read(run_id)?;
        Ok(claim.terminal_event.is_some()
            && claim.terminal_event_handed_off
            && (claim.state == "terminal" || claim.state == "failed"))
    }

    /// Retain the exact closing record before attempting its outbox handoff.
    /// The response event and outcome are never replaced by this transition.
    pub fn retain_invocation_close_event(
        &self,
        run_id: &str,
        event: Value,
    ) -> Result<Option<Value>> {
        anyhow::ensure!(
            event.get("run_id").and_then(Value::as_str) == Some(run_id)
                && event
                    .pointer("/payload/invocation/state")
                    .and_then(Value::as_str)
                    == Some("closed"),
            "invalid invocation closing event for run {run_id}"
        );
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if claim.invocation_close_event_handed_off {
            return Ok(None);
        }
        if claim.invocation_close_event.is_some() {
            return Ok(claim.invocation_close_event);
        }
        anyhow::ensure!(
            claim
                .terminal_event
                .as_ref()
                .and_then(|terminal| terminal.get("dedupe_key"))
                != event.get("dedupe_key"),
            "invocation close must have a distinct event identity"
        );
        claim.invocation_close_event = Some(event);
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim.invocation_close_event)
    }

    /// Commit closed only after the exact closing event is in the durable outbox.
    pub fn mark_invocation_close_event_handed_off(
        &self,
        run_id: &str,
        event: &Value,
    ) -> Result<bool> {
        let (_lock, mut claim) = self.read_for_update(run_id)?;
        if claim.invocation_close_event.as_ref() != Some(event) {
            return Ok(false);
        }
        if claim.invocation_close_event_handed_off {
            return Ok(true);
        }
        claim.invocation_close_event_handed_off = true;
        claim.invocation_state = Some("closed".to_string());
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(true)
    }

    pub fn read(&self, run_id: &str) -> Result<TurnClaim> {
        validate_id(run_id, "run_id")?;
        let path = self.claim_path(run_id);
        let bytes =
            fs::read(&path).with_context(|| format!("reading turn claim {}", path.display()))?;
        serde_json::from_slice(&bytes)
            .with_context(|| format!("parsing turn claim {}", path.display()))
    }

    /// Hold this run's stable sidecar lock from the fresh read through the
    /// atomic replacement, so concurrent callbacks cannot publish stale claims.
    fn read_for_update(&self, run_id: &str) -> Result<(fs::File, TurnClaim)> {
        let lock = self.lock_run(run_id)?;
        let claim = self.read(run_id)?;
        Ok((lock, claim))
    }

    fn lock_run(&self, run_id: &str) -> Result<fs::File> {
        validate_id(run_id, "run_id")?;
        self.ensure_root()?;
        let path = self.root.join(format!(".{run_id}.lock"));
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&path)
            .with_context(|| format!("opening turn claim lock {}", path.display()))?;
        set_private_file_permissions(&file)?;
        file.lock()
            .with_context(|| format!("locking turn claim {}", run_id))?;
        Ok(file)
    }

    fn write(&self, claim: &TurnClaim) -> Result<()> {
        self.ensure_root()?;
        let path = self.claim_path(&claim.run_id);
        let temporary = self
            .root
            .join(format!(".{}.{}.tmp", claim.run_id, Uuid::new_v4()));
        let bytes = serde_json::to_vec_pretty(claim)?;
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        set_private_file_permissions(&file)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, &path)
            .with_context(|| format!("replacing turn claim {}", path.display()))?;
        crate::outbox::sync_directory(&self.root)?;
        Ok(())
    }

    fn ensure_root(&self) -> Result<()> {
        fs::create_dir_all(&self.root)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))?;
        }
        Ok(())
    }

    fn claim_path(&self, run_id: &str) -> PathBuf {
        self.root.join(format!("{run_id}.json"))
    }
}

fn parse_claim_file(path: &std::path::Path, bytes: std::io::Result<Vec<u8>>) -> Option<TurnClaim> {
    let claim = bytes
        .map_err(anyhow::Error::from)
        .and_then(|bytes| serde_json::from_slice::<TurnClaim>(&bytes).map_err(anyhow::Error::from));
    match claim {
        Ok(claim) => Some(claim),
        Err(error) => {
            tracing::warn!(path = %path.display(), %error, "Skipping unreadable turn claim");
            None
        }
    }
}

fn validate_id(value: &str, label: &str) -> Result<()> {
    Uuid::parse_str(value)
        .with_context(|| format!("{label} must be a UUID"))
        .map(|_| ())
}

fn set_private_file_permissions(file: &fs::File) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

pub fn default_registry() -> Result<TurnClaimRegistry> {
    Ok(TurnClaimRegistry::new(
        crate::config::get_agent_dir()?.join("turn-claims"),
    ))
}

pub fn process_start_time_for_pid(pid: Option<u32>) -> Option<String> {
    let pid = pid?;
    crate::process_identity::try_collect_process_fact(pid).map(|fact| fact.lstart)
}

pub fn mark_terminal(run_id: &str, terminal_state: &str, error: Option<String>) {
    if let Ok(registry) = default_registry() {
        let _ = registry.mark_terminal(run_id, terminal_state, error);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unreadable_claim_registry_is_not_an_empty_ownership_inventory() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("claims");
        fs::write(&path, "not a directory").unwrap();
        assert!(TurnClaimRegistry::new(path).list_all().is_err());
        assert!(TurnClaimRegistry::new(dir.path().join("absent"))
            .list_all()
            .unwrap()
            .is_empty());
    }

    fn id(value: u128) -> String {
        Uuid::from_u128(value).to_string()
    }

    /// Recovery signals a recorded process group only when it can still prove
    /// the group is the one this claim spawned. A claim carried across a reboot
    /// cannot prove that — every pid is free again and the same number can name
    /// an unrelated group — and a claim written before the field existed cannot
    /// prove it either.
    #[test]
    fn recorded_process_group_is_trusted_only_within_its_own_boot() {
        let temp = tempfile::tempdir().unwrap();
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        let run_id = id(1);
        registry
            .claim(&run_id, &id(2), &id(3), None, None, "opencode")
            .unwrap();
        let spawned = registry
            .mark_spawned_invocation(
                &run_id,
                std::process::id(),
                i32::try_from(std::process::id()).unwrap(),
                None,
                "opencode_run",
                &id(4),
                None,
                "/tmp/out.log",
                "/tmp/err.log",
                serde_json::json!({}),
            )
            .unwrap();

        assert!(
            spawned.boot_id.is_some(),
            "a spawned claim must record the boot its pid belongs to"
        );
        assert!(spawned.process_group_is_from_this_boot());

        let mut rebooted = spawned.clone();
        rebooted.boot_id = Some("macos:1:0".to_string());
        assert!(!rebooted.process_group_is_from_this_boot());

        let mut legacy = spawned;
        legacy.boot_id = None;
        assert!(!legacy.process_group_is_from_this_boot());
    }

    fn age(root: &std::path::Path) {
        for entry in fs::read_dir(root).unwrap().flatten() {
            fs::OpenOptions::new()
                .write(true)
                .open(entry.path())
                .unwrap()
                .set_modified(std::time::SystemTime::now() - std::time::Duration::from_secs(60))
                .unwrap();
        }
    }

    /// A scan lists the registry once per source it examines; the claims only
    /// need reading again when one of them changed.
    #[test]
    fn listing_an_unchanged_registry_again_does_not_read_it_again() {
        let temp = tempfile::tempdir().unwrap();
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        registry
            .claim(&id(1), &id(2), &id(3), None, None, "codex")
            .unwrap();
        age(temp.path());

        let before = crate::dir_cache::FILES_PARSED.with(|parsed| parsed.get());
        let first = registry.list_all_shared().unwrap();
        for _ in 0..4 {
            assert!(std::sync::Arc::ptr_eq(
                &first,
                &registry.list_all_shared().unwrap()
            ));
        }
        assert_eq!(
            crate::dir_cache::FILES_PARSED.with(|parsed| parsed.get()) - before,
            1
        );
        assert_eq!(registry.list_all().unwrap().len(), 1);

        // A claim that moved on is what the next listing says.
        registry.mark_terminal(&id(1), "completed", None).unwrap();
        age(temp.path());
        let after = registry.list_all_shared().unwrap();
        assert_eq!(after.len(), 1);
        assert_eq!(after[0].state, "terminal");
    }

    #[test]
    fn duplicate_claim_returns_existing_without_reacquiring() {
        let temp = tempfile::tempdir().unwrap();
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        let run_id = id(1);
        let session_id = id(2);
        let thread_id = id(3);

        assert!(matches!(
            registry
                .claim(
                    &run_id,
                    &session_id,
                    &thread_id,
                    Some(&id(4)),
                    Some("request-1"),
                    "codex"
                )
                .unwrap(),
            ClaimOutcome::Acquired
        ));
        let duplicate = registry
            .claim(
                &run_id,
                &session_id,
                &thread_id,
                Some(&id(4)),
                Some("request-1"),
                "codex",
            )
            .unwrap();
        let ClaimOutcome::Existing(existing) = duplicate else {
            panic!("duplicate claim was reacquired");
        };
        assert_eq!(existing.state, "claimed");
        assert_eq!(existing.turn_id, Some(id(4)));
        assert_eq!(existing.client_request_id.as_deref(), Some("request-1"));
    }

    #[test]
    fn spawned_invocation_identity_and_cancel_survive_registry_recreation() {
        let temp = tempfile::tempdir().unwrap();
        let run_id = id(11);
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        registry
            .claim(&run_id, &id(12), &id(13), None, None, "cursor")
            .unwrap();
        registry
            .mark_spawned_invocation(
                &run_id,
                42,
                42,
                Some("Mon Jul 15 10:00:00 2026".to_string()),
                "cursor_print",
                "launch-11",
                Some("provider-thread-11"),
                "/tmp/stdout.jsonl",
                "/tmp/stderr.log",
                serde_json::json!({"transport": "cursor_print"}),
            )
            .unwrap();
        registry.mark_cancel_requested(&run_id).unwrap();

        let reopened = TurnClaimRegistry::new(temp.path().to_path_buf());
        let claim = reopened.read(&run_id).unwrap();
        assert_eq!(claim.state, "spawned");
        assert_eq!(claim.pid, Some(42));
        assert_eq!(claim.process_group_id, Some(42));
        assert_eq!(claim.adapter.as_deref(), Some("cursor_print"));
        assert_eq!(
            claim.provider_thread_id.as_deref(),
            Some("provider-thread-11")
        );
        assert_eq!(claim.stdout_path.as_deref(), Some("/tmp/stdout.jsonl"));
        assert!(claim.cancel_requested_at.is_some());
        assert_eq!(claim.projected_stdout_offset, 0);
        assert_eq!(claim.result.unwrap()["transport"], "cursor_print");
    }

    #[test]
    fn projection_checkpoint_survives_restart_and_never_moves_backwards() {
        let temp = tempfile::tempdir().unwrap();
        let run_id = id(41);
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        registry
            .claim(&run_id, &id(42), &id(43), None, None, "cursor")
            .unwrap();
        registry
            .mark_projection_checkpoint(&run_id, 128, 3)
            .unwrap();

        let reopened = TurnClaimRegistry::new(temp.path().to_path_buf());
        let claim = reopened.read(&run_id).unwrap();
        assert_eq!(claim.projected_stdout_offset, 128);
        assert_eq!(claim.projected_seq, 3);
        assert!(reopened.mark_projection_checkpoint(&run_id, 64, 4).is_err());
        assert!(reopened
            .mark_projection_checkpoint(&run_id, 256, 2)
            .is_err());
    }

    #[test]
    fn terminal_result_survives_registry_recreation() {
        let temp = tempfile::tempdir().unwrap();
        let run_id = id(21);
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        registry
            .claim(&run_id, &id(22), &id(23), None, None, "codex")
            .unwrap();
        registry
            .mark_spawned(
                &run_id,
                Some(42),
                Some(42),
                Some("start".to_string()),
                "codex_exec",
                serde_json::json!({"pid": 42}),
            )
            .unwrap();
        registry
            .mark_terminal(&run_id, "run_completed", None)
            .unwrap();

        let reopened = TurnClaimRegistry::new(temp.path().to_path_buf());
        let claim = reopened.read(&run_id).unwrap();
        assert_eq!(claim.state, "terminal");
        assert_eq!(claim.process_group_id, Some(42));
        assert_eq!(claim.adapter.as_deref(), Some("codex_exec"));
        assert_eq!(claim.result.unwrap()["terminal_state"], "run_completed");
    }

    #[test]
    fn concurrent_late_claim_update_preserves_terminal_handoff() {
        let temp = tempfile::tempdir().unwrap();
        let run_id = id(211);
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        registry
            .claim(&run_id, &id(212), &id(213), None, None, "claude")
            .unwrap();
        let event = serde_json::json!({
            "kind": "terminal_signal",
            "run_id": run_id.clone(),
            "payload": {
                "terminal_state": "run_completed",
                "invocation": {"state": "closed", "pending_count": 2}
            }
        });
        assert_eq!(
            registry
                .mark_terminal_with_event(&run_id, "run_completed", None, event.clone())
                .unwrap(),
            Some(event.clone())
        );
        assert!(registry
            .mark_terminal_event_handed_off(&run_id, &event)
            .unwrap());
        let repeated = registry
            .mark_terminal(&run_id, "run_failed", Some("late callback".to_string()))
            .unwrap();
        assert_eq!(repeated.state, "terminal");
        assert_eq!(repeated.terminal_event, Some(event.clone()));

        // Hold the same per-run lock used by every read-modify-write call while
        // the delayed lifecycle update starts. It cannot read a stale claim and
        // overwrite the acknowledged terminal evidence.
        let (claim_lock, snapshot) = registry.read_for_update(&run_id).unwrap();
        assert!(snapshot.terminal_event_handed_off);
        let writer_registry = registry.clone();
        let writer_run_id = run_id.clone();
        let (started_tx, started_rx) = std::sync::mpsc::channel();
        let (updated_tx, updated_rx) = std::sync::mpsc::channel();
        let writer = std::thread::spawn(move || {
            started_tx.send(()).unwrap();
            let claim = writer_registry
                .record_invocation_state(&writer_run_id, "closed", 2)
                .unwrap();
            updated_tx.send(claim).unwrap();
        });
        started_rx.recv().unwrap();
        assert!(
            updated_rx
                .recv_timeout(std::time::Duration::from_millis(25))
                .is_err(),
            "claim update must wait for the in-flight per-run transaction"
        );
        drop(claim_lock);

        let updated = updated_rx
            .recv_timeout(std::time::Duration::from_secs(1))
            .unwrap();
        writer.join().unwrap();
        assert_eq!(updated.terminal_event, Some(event.clone()));
        assert!(updated.terminal_event_handed_off);
        assert_eq!(updated.invocation_state.as_deref(), Some("closed"));
        assert_eq!(updated.pending_count, 2);
        assert!(registry.terminal_event_handed_off(&run_id).unwrap());
    }

    #[test]
    fn omp_pending_directory_preserves_claim_scope_without_native_identity() {
        let temp = tempfile::tempdir().unwrap();
        let run_id = id(241);
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        registry
            .claim(&run_id, &id(242), &id(243), None, None, "omp")
            .unwrap();
        let mut initial = registry.read(&run_id).unwrap();
        initial.result = Some(serde_json::json!({"launch_marker": "kept"}));
        registry.write(&initial).unwrap();

        let pending_dir = temp.path().join("omp-sessions/longhouse-scope");
        let pending = registry
            .set_pending_omp_session_dir(&run_id, &pending_dir)
            .unwrap();
        assert_eq!(pending.state, "claimed");
        assert!(pending.provider_thread_id.is_none());
        assert!(!pending.provider_identity_confirmed);
        assert!(pending.source_path.is_none());
        assert_eq!(pending.result.as_ref().unwrap()["launch_marker"], "kept");
        assert_eq!(
            pending.result.as_ref().unwrap()["session_dir"].as_str(),
            Some(pending_dir.to_str().unwrap())
        );
    }
    #[test]
    fn terminal_claim_without_spawn_stores_terminal_result() {
        let temp = tempfile::tempdir().unwrap();
        let run_id = id(24);
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        registry
            .claim(&run_id, &id(25), &id(26), None, None, "omp")
            .unwrap();

        let terminal = registry
            .mark_terminal(&run_id, "run_failed", Some("not started".to_string()))
            .unwrap();
        assert_eq!(terminal.state, "terminal");
        assert_eq!(
            terminal.result.as_ref().unwrap()["terminal_state"],
            "run_failed"
        );
        assert_eq!(
            TurnClaimRegistry::new(temp.path().to_path_buf())
                .read(&run_id)
                .unwrap()
                .result
                .unwrap()["terminal_state"],
            "run_failed"
        );
    }

    #[test]
    fn provider_binding_survives_registry_recreation() {
        let temp = tempfile::tempdir().unwrap();
        let run_id = id(31);
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        registry
            .claim(
                &run_id,
                &id(32),
                &id(33),
                Some(&id(34)),
                Some("request-31"),
                "codex",
            )
            .unwrap();
        registry
            .mark_provider_binding(&run_id, "provider-thread-31", Some("/tmp/rollout.jsonl"))
            .unwrap();

        let claim = TurnClaimRegistry::new(temp.path().to_path_buf())
            .read(&run_id)
            .unwrap();
        assert_eq!(
            claim.provider_thread_id.as_deref(),
            Some("provider-thread-31")
        );
        assert_eq!(claim.source_path.as_deref(), Some("/tmp/rollout.jsonl"));
    }
    #[test]
    fn late_terminal_signal_does_not_overwrite_failed_claim() {
        let temp = tempfile::tempdir().unwrap();
        let run_id = id(51);
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        registry
            .claim(&run_id, &id(52), &id(53), None, None, "claude")
            .unwrap();
        registry
            .mark_failed(&run_id, "provider disappeared")
            .unwrap();
        registry
            .mark_terminal(&run_id, "run_completed", None)
            .unwrap();
        let claim = registry.read(&run_id).unwrap();
        assert_eq!(claim.state, "failed");
        assert_eq!(claim.error.as_deref(), Some("provider disappeared"));
    }

    #[test]
    fn first_exact_terminal_event_is_not_vetoed_by_an_earlier_thin_fact() {
        let temp = tempfile::tempdir().unwrap();
        let run_id = id(71);
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        registry
            .claim(&run_id, &id(72), &id(73), None, None, "claude")
            .unwrap();
        registry
            .mark_terminal(&run_id, "run_cancelled", None)
            .unwrap();
        registry
            .mark_terminal_with_event(
                &run_id,
                "run_completed",
                None,
                serde_json::json!({
                    "kind": "terminal_signal",
                    "run_id": run_id,
                    "dedupe_key": "first-exact-terminal",
                    "payload": {"terminal_state": "run_completed"}
                }),
            )
            .unwrap();
        let reloaded = TurnClaimRegistry::new(temp.path().to_path_buf())
            .read(&run_id)
            .unwrap();
        assert_eq!(reloaded.result.unwrap()["terminal_state"], "run_completed");
        assert_eq!(
            reloaded.terminal_event.unwrap()["payload"]["terminal_state"],
            "run_completed"
        );
        assert!(!reloaded.terminal_event_handed_off);
    }

    #[test]
    fn cancelled_monitor_registration_releases_execution_owner() {
        let run_id = id(61);
        let monitor = register_monitor(&run_id);
        assert!(monitor_is_active(&run_id));
        assert!(cancel_monitor(&run_id));
        assert!(monitor_cancel_requested(&run_id));
        drop(monitor);
        assert!(!monitor_is_active(&run_id));
    }
}
