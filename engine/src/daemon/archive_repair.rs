//! The archive repair control file: pause, trickle or drain.

use super::*;

#[derive(Clone, Debug, Default, Deserialize)]
pub(super) struct ArchiveRepairControl {
    pub(super) mode: Option<String>,
    pub(super) expires_at: Option<String>,
    pub(super) actor: Option<String>,
    pub(super) reason: Option<String>,
    pub(super) updated_at: Option<String>,
}

impl ArchiveRepairControl {
    pub(super) fn active_override(&self) -> bool {
        let mode = self
            .mode
            .as_deref()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase();
        if matches!(mode.as_str(), "paused" | "pause") {
            return true;
        }
        let Some(expires_at) = self.expires_at.as_deref() else {
            return false;
        };
        chrono::DateTime::parse_from_rfc3339(expires_at)
            .map(|value| value.with_timezone(&chrono::Utc) > chrono::Utc::now())
            .unwrap_or(false)
    }

    pub(super) fn normalized_mode(&self, default_mode: ArchiveRepairMode) -> ArchiveRepairMode {
        if !self.active_override() {
            return default_mode;
        }
        match self
            .mode
            .as_deref()
            .unwrap_or(default_mode.as_str())
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "paused" | "pause" => ArchiveRepairMode::Paused,
            "trickle" | "resume" => ArchiveRepairMode::Trickle,
            "drain" | "drain-now" => ArchiveRepairMode::Drain,
            _ => default_mode,
        }
    }

    pub(super) fn is_paused(&self, default_mode: ArchiveRepairMode) -> bool {
        self.normalized_mode(default_mode).is_paused()
    }
}

pub(super) fn read_archive_repair_control() -> ArchiveRepairControl {
    let Ok(path) = config::get_agent_archive_repair_control_path() else {
        return ArchiveRepairControl::default();
    };
    let Ok(bytes) = std::fs::read(&path) else {
        return ArchiveRepairControl::default();
    };
    match serde_json::from_slice::<ArchiveRepairControl>(&bytes) {
        Ok(control) => control,
        Err(err) => {
            tracing::warn!(
                path = %path.display(),
                error = %err,
                "Ignoring invalid archive repair control file"
            );
            ArchiveRepairControl::default()
        }
    }
}

pub(super) fn apply_archive_repair_control(
    payload: &mut heartbeat::HeartbeatPayload,
    control: &ArchiveRepairControl,
    default_mode: ArchiveRepairMode,
) {
    let mode = control.normalized_mode(default_mode);
    payload.archive_backlog.mode = mode.as_str().to_string();
    payload.archive_backlog.pause_actor = None;
    payload.archive_backlog.pause_reason = None;
    payload.archive_backlog.pause_updated_at = None;
    // A pause holds the storage-v2 retry lane, so it is visible exactly while
    // that lane has envelopes waiting; with nothing held there is nothing paused.
    if mode == ArchiveRepairMode::Paused && payload.storage_v2_outbox.pending_count > 0 {
        payload.archive_backlog.state = "paused".to_string();
        payload.archive_backlog.pause_actor = control.actor.clone();
        payload.archive_backlog.pause_reason = control.reason.clone();
        payload.archive_backlog.pause_updated_at = control.updated_at.clone();
        return;
    }
    payload.archive_backlog.state = "complete".to_string();
}

pub(super) fn archive_repair_is_paused(default_mode: ArchiveRepairMode) -> bool {
    read_archive_repair_control().is_paused(default_mode)
}
