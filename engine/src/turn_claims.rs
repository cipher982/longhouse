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

const CLAIM_SCHEMA_VERSION: u32 = 2;

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
    pub provider: String,
    pub state: String,
    pub claimed_at: String,
    pub updated_at: String,
    pub pid: Option<u32>,
    pub process_start_time: Option<String>,
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
            provider: provider.to_string(),
            state: "claimed".to_string(),
            claimed_at: now.clone(),
            updated_at: now,
            pid: None,
            process_start_time: None,
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
    fn spawned_result_survives_registry_recreation() {
        let temp = tempfile::tempdir().unwrap();
        let run_id = id(11);
        let registry = TurnClaimRegistry::new(temp.path().to_path_buf());
        registry
            .claim(&run_id, &id(12), &id(13), None, None, "cursor")
            .unwrap();
        registry
            .mark_spawned(
                &run_id,
                Some(42),
                Some("Mon Jul 15 10:00:00 2026".to_string()),
                serde_json::json!({"transport": "cursor_acp"}),
            )
            .unwrap();

        let reopened = TurnClaimRegistry::new(temp.path().to_path_buf());
        let claim = reopened.read(&run_id).unwrap();
        assert_eq!(claim.state, "spawned");
        assert_eq!(claim.pid, Some(42));
        assert_eq!(claim.result.unwrap()["transport"], "cursor_acp");
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
}
