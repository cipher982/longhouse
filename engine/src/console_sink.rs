//! Envelope plumbing shared by the Console (headless, UI-driven) adapters.

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
