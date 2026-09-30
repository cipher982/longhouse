//! Parsed copies of small directories of JSON files (launch claims, turn
//! claims), so a scan pass that asks every source the same question reads the
//! directory once instead of once per source.
//!
//! A copy is trusted only while every entry still has the name, length,
//! inode and modification time it was parsed at, and only when nothing in the
//! directory was written in the last [`AT_REST_AFTER`]: a coarse filesystem
//! clock can give a second write inside one tick the same modification time,
//! so a file that was just written is read again next time, exactly as it was
//! before the cache existed. Checking costs one `readdir` and one `stat` per
//! entry and never reads a file.

use std::any::{Any, TypeId};
use std::collections::HashMap;
use std::ffi::OsString;
use std::fs;
use std::io::ErrorKind;
#[cfg(unix)]
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::SystemTime;

use sha2::{Digest, Sha256};

use crate::state::file_identity::AT_REST_AFTER;

/// Directories kept at once. Claim directories are few; the bound only keeps a
/// pathological caller from growing the map without limit.
const MAX_DIRECTORIES: usize = 64;

type Signature = Vec<(OsString, u64, u64, i128)>;

struct Entry {
    signature: Signature,
    parsed: Arc<dyn Any + Send + Sync>,
}

#[cfg(test)]
thread_local! {
    /// Files parsed on this thread, so a test can say a directory was not read
    /// again.
    pub(crate) static FILES_PARSED: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

thread_local! {
    static UNREAD_FILES: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

/// How many files (or directories) this thread has tried to read and could not,
/// ever. A caller that concludes something from a pass over these directories
/// and remembers the conclusion asks before and after: if the count moved, the
/// conclusion was drawn without something it could not see, and nothing a stat
/// can observe says when that stops being true (a `chmod` moves neither length,
/// inode nor mtime).
pub(crate) fn unread_files() -> u64 {
    UNREAD_FILES.with(|unread| unread.get())
}

/// A copy is for one directory read one way: the same files parsed or ordered
/// differently are a different copy. Function addresses can miss (one function
/// emitted twice reads as two, costing a re-parse) or merge (two identical
/// bodies read as one, and read the same), so neither can serve a wrong copy.
type CacheKey = (PathBuf, TypeId, usize, usize);

static CACHE: LazyLock<Mutex<HashMap<CacheKey, Entry>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// Parse every `*.json` file in `dir` into `order`ed values, or reuse the last
/// parse when none of them changed. A missing directory is an empty one.
/// `parse` sees the file's bytes, or the error that stopped them being read,
/// and returns `None` to leave the file out.
pub(crate) fn parsed_json_dir<T>(
    dir: &Path,
    parse: fn(&Path, std::io::Result<Vec<u8>>) -> Option<T>,
    order: fn(&T, &T) -> std::cmp::Ordering,
) -> std::io::Result<Arc<Vec<T>>>
where
    T: Send + Sync + 'static,
{
    let listing = match list_json(dir) {
        Ok(listing) => listing,
        Err(error) if error.kind() == ErrorKind::NotFound => {
            return Ok(Arc::new(Vec::new()));
        }
        Err(error) => {
            UNREAD_FILES.with(|unread| unread.set(unread.get() + 1));
            return Err(error);
        }
    };
    let key: CacheKey = (
        dir.to_path_buf(),
        TypeId::of::<T>(),
        parse as usize,
        order as usize,
    );
    if let Some(signature) = listing.signature.as_ref() {
        if let Ok(cache) = CACHE.lock() {
            if let Some(entry) = cache.get(&key) {
                if entry.signature == *signature {
                    if let Ok(parsed) = Arc::clone(&entry.parsed).downcast::<Vec<T>>() {
                        return Ok(parsed);
                    }
                }
            }
        }
    }
    // A file that could not be read is left out, and asked for again next time:
    // whatever stopped the read (a permission, say) can change with no change to
    // the name, length, inode or mtime that a copy is trusted on.
    let mut every_file_read = true;
    let mut parsed: Vec<T> = listing
        .paths
        .into_iter()
        .filter_map(|path| {
            #[cfg(test)]
            FILES_PARSED.with(|parsed| parsed.set(parsed.get() + 1));
            let bytes = fs::read(&path);
            if bytes.is_err() {
                every_file_read = false;
                UNREAD_FILES.with(|unread| unread.set(unread.get() + 1));
            }
            parse(&path, bytes)
        })
        .collect();
    parsed.sort_by(order);
    let parsed = Arc::new(parsed);
    let signature = listing.signature.filter(|_| every_file_read);
    if let (Some(signature), Ok(mut cache)) = (signature, CACHE.lock()) {
        if cache.len() >= MAX_DIRECTORIES && !cache.contains_key(&key) {
            cache.clear();
        }
        cache.insert(
            key,
            Entry {
                signature,
                parsed: parsed.clone(),
            },
        );
    }
    Ok(parsed)
}

/// A short signature of the `*.json` entries in `dir`, for a caller that keeps
/// its own conclusion drawn from them: the same signature later means the same
/// files. `None` when something in the directory was written too recently to
/// vouch for, or it cannot be read; a directory that does not exist has a
/// signature of its own. It is taken from stat alone, so it says nothing about
/// whether a file could be opened: a conclusion drawn while one could not is not
/// one to remember (see [`unread_files`]).
pub(crate) fn rested_signature(dir: &Path) -> Option<String> {
    match list_json(dir) {
        Ok(listing) => {
            let signature = listing.signature?;
            Some(format!("{:x}", Sha256::digest(format!("{signature:?}"))))
        }
        Err(error) if error.kind() == ErrorKind::NotFound => Some("absent".to_string()),
        Err(_) => None,
    }
}

struct Listing {
    paths: Vec<PathBuf>,
    /// `None` when something was written too recently to vouch for.
    signature: Option<Signature>,
}

fn list_json(dir: &Path) -> std::io::Result<Listing> {
    let now = SystemTime::now();
    let mut paths = Vec::new();
    let mut signature: Signature = Vec::new();
    let mut at_rest = true;
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) != Some("json") {
            continue;
        }
        // A file that vanished between the listing and the stat is a change
        // like any other: leave it out and let the next call see the directory.
        let Ok(metadata) = entry.metadata() else {
            at_rest = false;
            continue;
        };
        // The stat is of the link, not of what it points at, so a link's
        // signature says nothing about the content that gets read.
        at_rest &= !metadata.file_type().is_symlink();
        let modified = metadata.modified().ok();
        // A modification time in the future is a clock we cannot reason about.
        at_rest &= modified
            .and_then(|modified| now.duration_since(modified).ok())
            .is_some_and(|age| age >= AT_REST_AFTER);
        let modified_nanos = modified
            .and_then(|modified| modified.duration_since(SystemTime::UNIX_EPOCH).ok())
            .map(|elapsed| elapsed.as_nanos() as i128)
            .unwrap_or(-1);
        #[cfg(unix)]
        let inode = metadata.ino();
        #[cfg(not(unix))]
        let inode = 0;
        signature.push((entry.file_name(), metadata.len(), inode, modified_nanos));
        paths.push(path);
    }
    signature.sort();
    Ok(Listing {
        paths,
        signature: at_rest.then_some(signature),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn parse(_path: &Path, bytes: std::io::Result<Vec<u8>>) -> Option<String> {
        String::from_utf8(bytes.ok()?).ok()
    }

    fn files_parsed() -> usize {
        FILES_PARSED.with(|parsed| parsed.get())
    }

    fn age(path: &Path, seconds: u64) {
        let old = SystemTime::now() - Duration::from_secs(seconds);
        fs::OpenOptions::new()
            .write(true)
            .open(path)
            .unwrap()
            .set_modified(old)
            .unwrap();
    }

    #[test]
    fn a_directory_that_did_not_change_is_parsed_once() {
        let dir = tempfile::tempdir().unwrap();
        for name in ["a.json", "b.json"] {
            fs::write(dir.path().join(name), name).unwrap();
            age(&dir.path().join(name), 60);
        }
        fs::write(dir.path().join("ignored.txt"), "no").unwrap();
        let first = parsed_json_dir(dir.path(), parse, Ord::cmp).unwrap();
        assert_eq!(first.len(), 2);
        for _ in 0..3 {
            assert_eq!(
                parsed_json_dir(dir.path(), parse, Ord::cmp).unwrap().len(),
                2
            );
        }
        assert_eq!(files_parsed(), 2, "an unchanged directory was re-read");
    }

    #[test]
    fn a_rewritten_a_new_or_a_removed_file_is_read_again() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a.json");
        fs::write(&a, "one").unwrap();
        age(&a, 60);
        let values =
            |dir: &Path| -> Vec<String> { parsed_json_dir(dir, parse, Ord::cmp).unwrap().to_vec() };
        assert_eq!(values(dir.path()), ["one"]);

        // Same name and length, rewritten in place: only the mtime moved.
        fs::write(&a, "two").unwrap();
        age(&a, 30);
        assert_eq!(values(dir.path()), ["two"]);

        let b = dir.path().join("b.json");
        fs::write(&b, "new").unwrap();
        age(&b, 60);
        assert_eq!(values(dir.path()), ["new", "two"]);

        fs::remove_file(&a).unwrap();
        assert_eq!(values(dir.path()), ["new"]);
    }

    #[test]
    fn a_file_written_just_now_is_never_served_from_the_copy() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a.json");
        fs::write(&a, "fresh").unwrap();
        for _ in 0..3 {
            assert_eq!(
                parsed_json_dir(dir.path(), parse, Ord::cmp).unwrap().len(),
                1
            );
        }
        assert_eq!(files_parsed(), 3);
    }

    #[cfg(unix)]
    #[test]
    fn a_linked_file_is_never_served_from_the_copy() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("target.txt");
        fs::write(&target, "one").unwrap();
        age(&target, 60);
        let claims = dir.path().join("claims");
        fs::create_dir(&claims).unwrap();
        let link = claims.join("linked.json");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        // The link itself is old; only what it points at will change.
        let long_ago = SystemTime::now() - Duration::from_secs(60);
        let since_epoch = long_ago.duration_since(SystemTime::UNIX_EPOCH).unwrap();
        let time = libc::timespec {
            tv_sec: since_epoch.as_secs() as libc::time_t,
            tv_nsec: since_epoch.subsec_nanos() as _,
        };
        let path = std::ffi::CString::new(std::os::unix::ffi::OsStrExt::as_bytes(link.as_os_str()))
            .unwrap();
        let set = unsafe {
            libc::utimensat(
                libc::AT_FDCWD,
                path.as_ptr(),
                [time, time].as_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        assert_eq!(set, 0, "could not age the link");

        // What the link points at can change without the link changing.
        assert_eq!(
            parsed_json_dir(&claims, parse, Ord::cmp).unwrap().to_vec(),
            ["one"]
        );
        fs::write(&target, "two").unwrap();
        age(&target, 30);
        assert_eq!(
            parsed_json_dir(&claims, parse, Ord::cmp).unwrap().to_vec(),
            ["two"]
        );
    }

    #[test]
    fn the_same_directory_read_another_way_is_not_served_from_the_first_copy() {
        let dir = tempfile::tempdir().unwrap();
        for name in ["a.json", "b.json"] {
            fs::write(dir.path().join(name), name).unwrap();
            age(&dir.path().join(name), 60);
        }
        let ascending = |dir: &Path| parsed_json_dir(dir, parse, Ord::cmp).unwrap().to_vec();
        let descending = |dir: &Path| {
            parsed_json_dir(dir, parse, |left: &String, right: &String| right.cmp(left))
                .unwrap()
                .to_vec()
        };
        assert_eq!(ascending(dir.path()), ["a.json", "b.json"]);
        assert_eq!(descending(dir.path()), ["b.json", "a.json"]);
        assert_eq!(ascending(dir.path()), ["a.json", "b.json"]);
    }

    #[cfg(unix)]
    #[test]
    fn a_file_that_could_not_be_read_is_asked_for_again() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.json");
        fs::write(&path, "one").unwrap();
        age(&path, 60);
        fs::set_permissions(&path, fs::Permissions::from_mode(0o000)).unwrap();
        // A user that reads anything has no unreadable file to make.
        if fs::read(&path).is_ok() {
            return;
        }

        let unread = unread_files();
        for _ in 0..2 {
            assert!(parsed_json_dir(dir.path(), parse, Ord::cmp)
                .unwrap()
                .is_empty());
        }
        assert_eq!(files_parsed(), 2, "an unreadable file was cached as absent");
        assert_eq!(
            unread_files(),
            unread + 2,
            "a caller could not tell that a file was missed"
        );

        // Made readable again, with nothing a stat can see having changed.
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(
            parsed_json_dir(dir.path(), parse, Ord::cmp)
                .unwrap()
                .to_vec(),
            ["one"]
        );
        assert_eq!(
            unread_files(),
            unread + 2,
            "a file that was read was counted"
        );
    }

    #[test]
    fn a_missing_directory_is_an_empty_one() {
        let dir = tempfile::tempdir().unwrap();
        assert!(parsed_json_dir(&dir.path().join("absent"), parse, Ord::cmp)
            .unwrap()
            .is_empty());
    }
}
