//! Pieces shared by the per-provider managed-state scanners.
//!
//! Each `managed_*_scan` module reads one directory of per-session JSON state
//! files and turns them into observations. The enumeration step is the same
//! for all of them: four modules held byte-identical copies of it and a fifth
//! spelled the same filter inline.

use std::fs;
use std::path::{Path, PathBuf};

/// Every `*.json` file directly inside `state_dir`, in whatever order the
/// filesystem hands them back. An unreadable directory yields nothing: these
/// scanners run on a timer, so the next pass sees it if it comes back.
pub fn state_file_paths(state_dir: &Path) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(state_dir) else {
        return Vec::new();
    };
    entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("json"))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_json_files_in_the_directory_itself_are_returned() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(temp.path().join("a.json"), "{}").unwrap();
        fs::write(temp.path().join("b.json"), "{}").unwrap();
        fs::write(temp.path().join("notes.txt"), "x").unwrap();
        fs::create_dir(temp.path().join("nested")).unwrap();
        fs::write(temp.path().join("nested/c.json"), "{}").unwrap();

        let mut found = state_file_paths(temp.path())
            .into_iter()
            .map(|path| path.file_name().unwrap().to_string_lossy().to_string())
            .collect::<Vec<_>>();
        found.sort();
        assert_eq!(found, vec!["a.json", "b.json"]);
    }

    #[test]
    fn a_missing_directory_is_empty_not_an_error() {
        let temp = tempfile::tempdir().unwrap();
        assert!(state_file_paths(&temp.path().join("absent")).is_empty());
    }
}
