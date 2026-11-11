//! Sources written recently, re-stated on a short tick so a lost file event
//! costs seconds, not a reconciliation interval.
//!
//! The watcher's channel drops events when it is full, and FSEvents and inotify
//! can coalesce or lose them. Before this, a lost event for an unmanaged
//! transcript waited for the reconciliation scan (300 s by default, and none at
//! all while Live work was pending). A session being written is almost always
//! one that was written in the last few minutes, so re-stating just those every
//! [`HOT_RECHECK_INTERVAL`] repairs the common case cheaply. Taken from Atuin
//! Threads' tree watcher (spec: session-capture-hardening.md, task 2).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

/// How often hot sources are re-stated.
pub(super) const HOT_RECHECK_INTERVAL: Duration = Duration::from_secs(2);
/// How long a source stays hot after its last write.
pub(super) const HOT_WINDOW: Duration = Duration::from_secs(15 * 60);
/// More than this many hot sources sheds the least recently written.
const HOT_SOURCES_MAX: usize = 2048;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Stat {
    len: u64,
    modified: SystemTime,
    inode: u64,
}

fn stat(path: &Path) -> Option<Stat> {
    let metadata = std::fs::metadata(path).ok()?;
    #[cfg(unix)]
    let inode = std::os::unix::fs::MetadataExt::ino(&metadata);
    #[cfg(not(unix))]
    let inode = 0;
    Some(Stat {
        len: metadata.len(),
        modified: metadata.modified().ok()?,
        inode,
    })
}

struct HotSource {
    provider: &'static str,
    stat: Stat,
}

#[derive(Default)]
pub(super) struct HotSources {
    entries: HashMap<PathBuf, HotSource>,
}

impl HotSources {
    pub(super) fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    #[cfg(test)]
    pub(super) fn len(&self) -> usize {
        self.entries.len()
    }

    /// Note a source that was just enqueued for shipping. Its stat is taken
    /// now: the job that was enqueued reads at least this much, so only a
    /// change after this moment is news to the re-check.
    pub(super) fn note(&mut self, path: PathBuf, provider: &'static str, now: SystemTime) {
        let Some(stat) = stat(&path) else {
            self.entries.remove(&path);
            return;
        };
        if !is_hot(stat.modified, now) {
            return;
        }
        self.entries.insert(path, HotSource { provider, stat });
        self.shed(now);
    }

    /// Re-state every hot source. Returns those that moved since they were
    /// last seen, and forgets those gone or quiet for the hot window.
    pub(super) fn recheck(&mut self, now: SystemTime) -> Vec<(PathBuf, &'static str)> {
        let mut moved = Vec::new();
        self.entries.retain(|path, source| {
            let Some(current) = stat(path) else {
                return false;
            };
            if current != source.stat {
                source.stat = current;
                moved.push((path.clone(), source.provider));
            }
            is_hot(current.modified, now)
        });
        moved
    }

    fn shed(&mut self, now: SystemTime) {
        if self.entries.len() <= HOT_SOURCES_MAX {
            return;
        }
        let mut by_age: Vec<(Duration, PathBuf)> = self
            .entries
            .iter()
            .map(|(path, source)| {
                (
                    now.duration_since(source.stat.modified).unwrap_or_default(),
                    path.clone(),
                )
            })
            .collect();
        by_age.sort();
        for (_, path) in by_age.into_iter().skip(HOT_SOURCES_MAX) {
            self.entries.remove(&path);
        }
    }
}

fn is_hot(modified: SystemTime, now: SystemTime) -> bool {
    now.duration_since(modified)
        .map_or(true, |age| age <= HOT_WINDOW)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn an_append_after_noting_is_reported_once() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.jsonl");
        std::fs::write(&path, b"one\n").unwrap();
        let mut hot = HotSources::default();
        hot.note(path.clone(), "claude", SystemTime::now());
        assert!(hot.recheck(SystemTime::now()).is_empty());

        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        file.write_all(b"two\n").unwrap();
        drop(file);
        assert_eq!(
            hot.recheck(SystemTime::now()),
            vec![(path.clone(), "claude")]
        );
        assert!(hot.recheck(SystemTime::now()).is_empty());
    }

    #[test]
    fn a_same_length_rewrite_is_reported() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.jsonl");
        std::fs::write(&path, b"one\n").unwrap();
        let mut hot = HotSources::default();
        let earlier = SystemTime::now();
        hot.note(path.clone(), "codex", earlier);
        // Force a different mtime even on a coarse-clock filesystem.
        std::fs::write(&path, b"uno\n").unwrap();
        let file = std::fs::File::options().write(true).open(&path).unwrap();
        file.set_modified(earlier + Duration::from_secs(1)).unwrap();
        assert_eq!(hot.recheck(SystemTime::now()).len(), 1);
    }

    #[test]
    fn quiet_and_deleted_sources_are_forgotten() {
        let dir = tempfile::tempdir().unwrap();
        let quiet = dir.path().join("quiet.jsonl");
        let gone = dir.path().join("gone.jsonl");
        std::fs::write(&quiet, b"x\n").unwrap();
        std::fs::write(&gone, b"x\n").unwrap();
        let mut hot = HotSources::default();
        let now = SystemTime::now();
        hot.note(quiet.clone(), "claude", now);
        hot.note(gone.clone(), "claude", now);
        std::fs::remove_file(&gone).unwrap();
        assert!(hot.recheck(now).is_empty());
        assert_eq!(hot.len(), 1);
        assert!(hot
            .recheck(now + HOT_WINDOW + Duration::from_secs(5))
            .is_empty());
        assert!(hot.is_empty());
    }

    #[test]
    fn a_source_older_than_the_window_is_not_noted() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("old.jsonl");
        std::fs::write(&path, b"x\n").unwrap();
        let mut hot = HotSources::default();
        hot.note(path, "claude", SystemTime::now() + HOT_WINDOW * 2);
        assert!(hot.is_empty());
    }
}
