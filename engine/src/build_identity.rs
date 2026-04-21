//! Compiled-in build identity. All fields come from `env!(...)` populated
//! by `build.rs`; the build fails if any field is missing.

use serde::Serialize;

pub const VERSION: &str = env!("LONGHOUSE_BUILD_VERSION");
pub const COMMIT: &str = env!("LONGHOUSE_BUILD_COMMIT");
pub const COMMIT_SHORT: &str = env!("LONGHOUSE_BUILD_COMMIT_SHORT");
pub const BUILT_AT: &str = env!("LONGHOUSE_BUILD_BUILT_AT");
pub const CHANNEL: &str = env!("LONGHOUSE_BUILD_CHANNEL");
pub const DIRTY_RAW: &str = env!("LONGHOUSE_BUILD_DIRTY");

pub fn dirty() -> bool {
    matches!(DIRTY_RAW, "true" | "True" | "1")
}

#[derive(Debug, Clone, Serialize)]
pub struct BuildIdentity {
    pub version: &'static str,
    pub commit: &'static str,
    pub commit_short: &'static str,
    pub dirty: bool,
    pub built_at: &'static str,
    pub channel: &'static str,
}

impl BuildIdentity {
    pub fn current() -> Self {
        BuildIdentity {
            version: VERSION,
            commit: COMMIT,
            commit_short: COMMIT_SHORT,
            dirty: dirty(),
            built_at: BUILT_AT,
            channel: CHANNEL,
        }
    }

    /// Display format matching Python: "0.2.0 (b672fcca)" for release,
    /// "0.2.0-dev+b672fcca[.dirty]" for dev builds.
    pub fn qualified(&self) -> String {
        if self.channel == "release" {
            format!("{} ({})", self.version, self.commit_short)
        } else if self.dirty {
            format!("{}-dev+{}.dirty", self.version, self.commit_short)
        } else {
            format!("{}-dev+{}", self.version, self.commit_short)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn qualified_release_format() {
        let id = BuildIdentity {
            version: "0.2.0",
            commit: "b672fccae990",
            commit_short: "b672fcca",
            dirty: false,
            built_at: "2026-04-21T18:03:12Z",
            channel: "release",
        };
        assert_eq!(id.qualified(), "0.2.0 (b672fcca)");
    }

    #[test]
    fn qualified_dev_clean() {
        let id = BuildIdentity {
            version: "0.2.0",
            commit: "b672fccae990",
            commit_short: "b672fcca",
            dirty: false,
            built_at: "2026-04-21T18:03:12Z",
            channel: "dev",
        };
        assert_eq!(id.qualified(), "0.2.0-dev+b672fcca");
    }

    #[test]
    fn qualified_dev_dirty() {
        let id = BuildIdentity {
            version: "0.2.0",
            commit: "b672fccae990",
            commit_short: "b672fcca",
            dirty: true,
            built_at: "2026-04-21T18:03:12Z",
            channel: "dev",
        };
        assert_eq!(id.qualified(), "0.2.0-dev+b672fcca.dirty");
    }
}
