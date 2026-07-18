use std::fs;
use std::fs::OpenOptions;
use std::io::ErrorKind;
use std::io::Write;
use std::path::PathBuf;

use anyhow::Context;
use anyhow::Result;
use chrono::Utc;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use uuid::Uuid;

const CLAIM_SCHEMA_VERSION: u32 = 4;

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
    pub source_path: Option<String>,
    pub provider: String,
    pub state: String,
    pub claimed_at: String,
    pub updated_at: String,
    pub pid: Option<u32>,
    #[serde(default)]
    pub process_group_id: Option<i32>,
    pub process_start_time: Option<String>,
    #[serde(default)]
    pub adapter: Option<String>,
    #[serde(default)]
    pub launch_id: Option<String>,
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
    pub result: Option<Value>,
    pub error: Option<String>,
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
        self.ensure_root()?;
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
            source_path: None,
            provider: provider.to_string(),
            state: "claimed".to_string(),
            claimed_at: now.clone(),
            updated_at: now,
            pid: None,
            process_group_id: None,
            process_start_time: None,
            adapter: None,
            launch_id: None,
            stdout_path: None,
            stderr_path: None,
            cancel_requested_at: None,
            projected_stdout_offset: 0,
            projected_seq: 0,
            result: None,
            error: None,
        };
        let bytes = serde_json::to_vec_pretty(&claim)?;
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                set_private_file_permissions(&file)?;
                file.write_all(&bytes)?;
                file.sync_all()?;
                Ok(ClaimOutcome::Acquired)
            }
            Err(err) if err.kind() == ErrorKind::AlreadyExists => {
                Ok(ClaimOutcome::Existing(self.read(run_id)?))
            }
            Err(err) => Err(err).with_context(|| format!("creating turn claim {}", path.display())),
        }
    }

    pub fn mark_spawned(
        &self,
        run_id: &str,
        pid: Option<u32>,
        process_start_time: Option<String>,
        result: Value,
    ) -> Result<TurnClaim> {
        let mut claim = self.read(run_id)?;
        claim.state = "spawned".to_string();
        claim.pid = pid;
        claim.process_start_time = process_start_time;
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
        let mut claim = self.read(run_id)?;
        claim.state = "spawned".to_string();
        claim.pid = Some(pid);
        claim.process_group_id = Some(process_group_id);
        claim.process_start_time = process_start_time;
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

    pub fn mark_cancel_requested(&self, run_id: &str) -> Result<TurnClaim> {
        let mut claim = self.read(run_id)?;
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
        let mut claim = self.read(run_id)?;
        if stdout_offset < claim.projected_stdout_offset || seq < claim.projected_seq {
            anyhow::bail!("turn claim {run_id} projection checkpoint cannot move backwards");
        }
        claim.projected_stdout_offset = stdout_offset;
        claim.projected_seq = seq;
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn list_nonterminal(&self) -> Result<Vec<TurnClaim>> {
        let Ok(entries) = fs::read_dir(&self.root) else {
            return Ok(Vec::new());
        };
        let mut claims = Vec::new();
        for entry in entries {
            let path = entry?.path();
            if path.extension().and_then(|value| value.to_str()) != Some("json") {
                continue;
            }
            let bytes = fs::read(&path)?;
            let claim: TurnClaim = serde_json::from_slice(&bytes)
                .with_context(|| format!("parsing turn claim {}", path.display()))?;
            if claim.state != "terminal" && claim.state != "failed" {
                claims.push(claim);
            }
        }
        claims.sort_by(|left, right| left.claimed_at.cmp(&right.claimed_at));
        Ok(claims)
    }

    pub fn mark_provider_binding(
        &self,
        run_id: &str,
        provider_thread_id: &str,
        source_path: Option<&str>,
    ) -> Result<TurnClaim> {
        let mut claim = self.read(run_id)?;
        claim.provider_thread_id = Some(provider_thread_id.to_string());
        claim.source_path = source_path.map(str::to_string);
        claim.updated_at = Utc::now().to_rfc3339();
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn mark_failed(&self, run_id: &str, error: &str) -> Result<TurnClaim> {
        let mut claim = self.read(run_id)?;
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
        let mut claim = self.read(run_id)?;
        claim.state = "terminal".to_string();
        claim.error = error;
        claim.updated_at = Utc::now().to_rfc3339();
        if let Some(result) = claim.result.as_mut().and_then(Value::as_object_mut) {
            result.insert(
                "terminal_state".to_string(),
                Value::String(terminal_state.to_string()),
            );
        }
        self.write(&claim)?;
        Ok(claim)
    }

    pub fn read(&self, run_id: &str) -> Result<TurnClaim> {
        validate_id(run_id, "run_id")?;
        let path = self.claim_path(run_id);
        let bytes =
            fs::read(&path).with_context(|| format!("reading turn claim {}", path.display()))?;
        serde_json::from_slice(&bytes)
            .with_context(|| format!("parsing turn claim {}", path.display()))
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
    crate::process_identity::collect_process_facts_by_pid()
        .get(&pid)
        .map(|fact| fact.lstart.clone())
}

pub fn mark_terminal(run_id: &str, terminal_state: &str, error: Option<String>) {
    if let Ok(registry) = default_registry() {
        let _ = registry.mark_terminal(run_id, terminal_state, error);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(value: u128) -> String {
        Uuid::from_u128(value).to_string()
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
                Some("start".to_string()),
                serde_json::json!({"pid": 42}),
            )
            .unwrap();
        registry
            .mark_terminal(&run_id, "run_completed", None)
            .unwrap();

        let reopened = TurnClaimRegistry::new(temp.path().to_path_buf());
        let claim = reopened.read(&run_id).unwrap();
        assert_eq!(claim.state, "terminal");
        assert_eq!(claim.result.unwrap()["terminal_state"], "run_completed");
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
}
