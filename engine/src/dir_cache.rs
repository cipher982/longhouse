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
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, LazyLock, Mutex, RwLock};
use std::time::{Duration, Instant, SystemTime};

use sha2::{Digest, Sha256};

use crate::state::file_identity::AT_REST_AFTER;

/// Directories kept at once. Claim directories are few; the bound only keeps a
/// pathological caller from growing the map without limit.
const MAX_DIRECTORIES: usize = 64;

/// Safety re-verification horizon for FSEvents-watched directories.
/// Normal changes invalidate immediately via `notify_watched_dir_changed` and
/// directory `(ino, mtime, ctime)`; this cap also catches hypothetical missed
/// OS watcher events on in-place file rewrites without expiring every 15 s
/// between idle observation ticks.
const WATCHED_DIR_REVERIFY_INTERVAL: Duration = Duration::from_secs(60);

type Signature = Vec<(OsString, u64, u64, i128)>;
type DirStamp = (u64, i128, i128);

/// Per-root generation counters for directories watched by `SessionWatcher`.
/// Both the original path and its canonicalized path share one `Arc<AtomicU64>`
/// so symlinked aliases (e.g. `/tmp` vs `/private/tmp`) invalidate together
/// without disturbing unrelated watched directories.
static WATCHED_ROOTS: LazyLock<RwLock<Vec<(PathBuf, Arc<AtomicU64>)>>> =
    LazyLock::new(|| RwLock::new(Vec::new()));

/// Register directory roots actively watched recursively by `SessionWatcher`.
/// Cached snapshots under these roots skip per-entry `readdir`/`getattrlistbulk`
/// when the directory's own metadata and its root's watch generation have not
/// moved.
pub(crate) fn register_watched_dirs(dirs: &[PathBuf]) {
    if let Ok(mut roots) = WATCHED_ROOTS.write() {
        for dir in dirs {
            let canonical = fs::canonicalize(dir).unwrap_or_else(|_| dir.clone());
            let counter = roots
                .iter()
                .find(|(existing, _)| existing == &canonical || existing == dir)
                .map(|(_, gen)| Arc::clone(gen))
                .unwrap_or_else(|| Arc::new(AtomicU64::new(1)));
            counter.fetch_add(1, Ordering::AcqRel);
            if !roots.iter().any(|(existing, _)| existing == &canonical) {
                roots.push((canonical, Arc::clone(&counter)));
            }
            if !roots.iter().any(|(existing, _)| existing == dir) {
                roots.push((dir.clone(), counter));
            }
        }
    }
}

/// Signal that the filesystem watcher observed a change under a watched state
/// directory, bumping only the matching watched root(s) so unrelated state
/// directories keep their fast-path cache hits.
/// Safe to invoke directly from the OS watcher callback thread before event
/// coalescing or channel bounds can delay or drop the notification.
pub(crate) fn notify_watched_dir_changed(changed_path: &Path) {
    let Ok(roots) = WATCHED_ROOTS.read() else {
        return;
    };
    if roots.is_empty() {
        return;
    }
    let mut bumped: Vec<*const AtomicU64> = Vec::new();
    for (root, counter) in roots.iter() {
        if changed_path.starts_with(root) {
            let ptr = Arc::as_ptr(counter);
            if !bumped.contains(&ptr) {
                counter.fetch_add(1, Ordering::AcqRel);
                bumped.push(ptr);
            }
        }
    }
}

fn watched_root_counter(dir: &Path) -> Option<Arc<AtomicU64>> {
    let roots = WATCHED_ROOTS.read().ok()?;
    if roots.is_empty() {
        return None;
    }
    // Prefer the most specific (longest) registered root matching `dir`.
    roots
        .iter()
        .filter(|(root, _)| dir.starts_with(root))
        .max_by_key(|(root, _)| root.as_os_str().len())
        .map(|(_, counter)| Arc::clone(counter))
}

fn dir_rested_stamp(dir: &Path, now: SystemTime) -> Option<DirStamp> {
    let meta = fs::metadata(dir).ok()?;
    let modified = meta.modified().ok()?;
    if !now
        .duration_since(modified)
        .is_ok_and(|age| age >= AT_REST_AFTER)
    {
        return None;
    }
    let modified_nanos = modified
        .duration_since(SystemTime::UNIX_EPOCH)
        .ok()
        .map(|d| d.as_nanos() as i128)
        .unwrap_or(-1);
    #[cfg(unix)]
    let (inode, ctime_nanos) = (
        meta.ino(),
        (meta.ctime() as i128)
            .saturating_mul(1_000_000_000)
            .saturating_add(meta.ctime_nsec() as i128),
    );
    #[cfg(not(unix))]
    let (inode, ctime_nanos) = (0, -1);
    Some((inode, modified_nanos, ctime_nanos))
}

struct Entry {
    /// The whole directory's signature when it was at rest and every file
    /// read: an equal listing returns `parsed` without touching any file.
    signature: Option<Signature>,
    /// Fast-check stamp for FSEvents-watched directories: `(inode, mtime, ctime)`
    /// plus the `WATCH_GENERATION` observed before listing.
    dir_stamp: Option<DirStamp>,
    watch_gen: Option<u64>,
    verified_at: Instant,
    parsed: Arc<dyn Any + Send + Sync>,
    /// Each file's parse by path, kept only for files that were at rest when
    /// read, with the stamp they were read at (a `Files<T>`).
    files: Arc<dyn Any + Send + Sync>,
}

/// A file's (length, inode, modification nanos) as listed.
type Stamp = (u64, u64, i128);

type Files<T> = HashMap<PathBuf, (Stamp, Option<Arc<T>>)>;

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
///
/// When some files changed, only those are read again: a file still at rest
/// with the stamp it was parsed at keeps its parse. Turn-claim directories
/// hold hundreds of settled claims while one live turn rewrites its own, and
/// re-reading every file on each such write was a leading engine cost.
/// Parses are shared: a reused file costs a reference count, not a deep
/// copy, so a directory where one live session rewrites its file among
/// hundreds of settled ones no longer clones every settled parse per call.
/// Each returned snapshot is immutable; later changes build a new one.
pub(crate) fn parsed_json_dir<T>(
    dir: &Path,
    parse: fn(&Path, std::io::Result<Vec<u8>>) -> Option<T>,
    order: fn(&T, &T) -> std::cmp::Ordering,
) -> std::io::Result<Arc<Vec<Arc<T>>>>
where
    T: Send + Sync + 'static,
{
    let key: CacheKey = (
        dir.to_path_buf(),
        TypeId::of::<T>(),
        parse as usize,
        order as usize,
    );
    let watch_counter = watched_root_counter(dir);
    let watch_gen_before = watch_counter
        .as_ref()
        .map(|counter| counter.load(Ordering::Acquire));
    let dir_stamp = if watch_counter.is_some() {
        dir_rested_stamp(dir, SystemTime::now())
    } else {
        None
    };
    if watch_gen_before.is_some() && dir_stamp.is_some() {
        if let Ok(cache) = CACHE.lock() {
            if let Some(entry) = cache.get(&key) {
                if entry.signature.is_some()
                    && entry.dir_stamp == dir_stamp
                    && entry.watch_gen == watch_gen_before
                    && entry.verified_at.elapsed() < WATCHED_DIR_REVERIFY_INTERVAL
                {
                    if let Ok(parsed) = Arc::clone(&entry.parsed).downcast::<Vec<Arc<T>>>() {
                        return Ok(parsed);
                    }
                }
            }
        }
    }
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
    let mut previous_files: Option<Arc<Files<T>>> = None;
    if let Ok(mut cache) = CACHE.lock() {
        if let Some(entry) = cache.get_mut(&key) {
            if listing.signature.is_some() && entry.signature == listing.signature {
                if let (Some(counter), Some(gen_before)) = (&watch_counter, watch_gen_before) {
                    if counter.load(Ordering::Acquire) == gen_before {
                        entry.dir_stamp = dir_stamp;
                        entry.watch_gen = Some(gen_before);
                        entry.verified_at = Instant::now();
                    }
                }
                if let Ok(parsed) = Arc::clone(&entry.parsed).downcast::<Vec<Arc<T>>>() {
                    return Ok(parsed);
                }
            }
            previous_files = Arc::clone(&entry.files).downcast::<Files<T>>().ok();
        }
    }
    // A file that could not be read is left out, and asked for again next time:
    // whatever stopped the read (a permission, say) can change with no change to
    // the name, length, inode or mtime that a copy is trusted on.
    let mut every_file_read = true;
    let mut files: Files<T> = HashMap::with_capacity(listing.files.len());
    let mut parsed: Vec<Arc<T>> = Vec::with_capacity(listing.files.len());
    for file in listing.files {
        let reused = file
            .rested
            .then(|| previous_files.as_ref()?.get(&file.path))
            .flatten()
            .filter(|(stamp, _)| *stamp == file.stamp)
            .map(|(_, value)| value.clone());
        let value = match reused {
            Some(value) => value,
            None => {
                #[cfg(test)]
                FILES_PARSED.with(|parsed| parsed.set(parsed.get() + 1));
                let bytes = fs::read(&file.path);
                let read = bytes.is_ok();
                if !read {
                    every_file_read = false;
                    UNREAD_FILES.with(|unread| unread.set(unread.get() + 1));
                }
                let value = parse(&file.path, bytes).map(Arc::new);
                if !(read && file.rested) {
                    if let Some(value) = value {
                        parsed.push(value);
                    }
                    continue;
                }
                value
            }
        };
        if let Some(value) = value.as_ref() {
            parsed.push(Arc::clone(value));
        }
        files.insert(file.path, (file.stamp, value));
    }
    parsed.sort_by(|left, right| order(left, right));
    let parsed = Arc::new(parsed);
    if let Ok(mut cache) = CACHE.lock() {
        if cache.len() >= MAX_DIRECTORIES && !cache.contains_key(&key) {
            cache.clear();
        }
        let watch_gen = match (&watch_counter, watch_gen_before) {
            (Some(counter), Some(gen_before)) if counter.load(Ordering::Acquire) == gen_before => {
                Some(gen_before)
            }
            _ => None,
        };
        cache.insert(
            key,
            Entry {
                signature: listing.signature.filter(|_| every_file_read),
                dir_stamp: watch_gen.and(dir_stamp),
                watch_gen,
                verified_at: Instant::now(),
                parsed: parsed.clone(),
                files: Arc::new(files),
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

struct ListedFile {
    path: PathBuf,
    stamp: Stamp,
    /// Written long enough ago (and not a symlink) for its stamp to vouch
    /// for its content.
    rested: bool,
}

struct Listing {
    files: Vec<ListedFile>,
    /// `None` when something was written too recently to vouch for.
    signature: Option<Signature>,
}

fn list_json(dir: &Path) -> std::io::Result<Listing> {
    let now = SystemTime::now();
    let mut files = Vec::new();
    let mut signature: Signature = Vec::new();
    let mut at_rest = true;
    for entry in read_entries(dir)? {
        if Path::new(&entry.name)
            .extension()
            .and_then(|ext| ext.to_str())
            != Some("json")
        {
            continue;
        }
        // A file that vanished between the listing and the stat is a change
        // like any other: leave it out and let the next call see the directory.
        let Some(stat) = entry.stat else {
            at_rest = false;
            continue;
        };
        // The stat is of the link, not of what it points at, so a link's
        // signature says nothing about the content that gets read.
        let mut rested = !stat.is_symlink;
        // A modification time in the future is a clock we cannot reason about.
        rested &= stat
            .modified
            .and_then(|modified| now.duration_since(modified).ok())
            .is_some_and(|age| age >= AT_REST_AFTER);
        at_rest &= rested;
        let modified_nanos = stat
            .modified
            .and_then(|modified| modified.duration_since(SystemTime::UNIX_EPOCH).ok())
            .map(|elapsed| elapsed.as_nanos() as i128)
            .unwrap_or(-1);
        let stamp = (stat.len, stat.inode, modified_nanos);
        signature.push((entry.name.clone(), stamp.0, stamp.1, stamp.2));
        files.push(ListedFile {
            path: dir.join(&entry.name),
            stamp,
            rested,
        });
    }
    signature.sort();
    Ok(Listing {
        files,
        signature: at_rest.then_some(signature),
    })
}

struct RawEntry {
    name: OsString,
    /// `None` when the entry could not be stat'ed (it vanished, say).
    stat: Option<RawStat>,
}

struct RawStat {
    len: u64,
    inode: u64,
    modified: Option<SystemTime>,
    is_symlink: bool,
}

/// Every entry of `dir` with its own (not followed) length, inode and
/// modification time.
#[cfg(target_os = "macos")]
fn read_entries(dir: &Path) -> std::io::Result<Vec<RawEntry>> {
    bulk::read_entries(dir).or_else(|_| read_entries_portable(dir))
}

#[cfg(not(target_os = "macos"))]
fn read_entries(dir: &Path) -> std::io::Result<Vec<RawEntry>> {
    read_entries_portable(dir)
}

/// `*.json` entries of `dir`, stat'ed one by one. Other names are skipped
/// before the stat: this directory may hold logs and locks too.
fn read_entries_portable(dir: &Path) -> std::io::Result<Vec<RawEntry>> {
    let mut entries = Vec::new();
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let name = entry.file_name();
        if Path::new(&name).extension().and_then(|ext| ext.to_str()) != Some("json") {
            continue;
        }
        let stat = entry.metadata().ok().map(|metadata| RawStat {
            len: metadata.len(),
            #[cfg(unix)]
            inode: metadata.ino(),
            #[cfg(not(unix))]
            inode: 0,
            modified: metadata.modified().ok(),
            is_symlink: metadata.file_type().is_symlink(),
        });
        entries.push(RawEntry { name, stat });
    }
    Ok(entries)
}

/// `getattrlistbulk(2)`: names, types, inodes, sizes and modification times
/// of a whole directory in a few syscalls. `read_dir` plus a per-entry stat
/// was one `fstatat` per file, and the OMP Helm state directory (hundreds of
/// retained launches) is listed on every OMP shipping job.
#[cfg(target_os = "macos")]
mod bulk {
    use super::{RawEntry, RawStat};
    use std::ffi::{CString, OsString};
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    use std::path::Path;
    use std::time::{Duration, SystemTime};

    const COMMON: libc::attrgroup_t = libc::ATTR_CMN_RETURNED_ATTRS
        | libc::ATTR_CMN_NAME
        | libc::ATTR_CMN_OBJTYPE
        | libc::ATTR_CMN_MODTIME
        | libc::ATTR_CMN_FILEID;
    const FILE: libc::attrgroup_t = libc::ATTR_FILE_DATALENGTH;
    const VREG: u32 = 1;
    const VLNK: u32 = 5;

    fn read_u32(buffer: &[u8], at: usize) -> Option<u32> {
        Some(u32::from_ne_bytes(buffer.get(at..at + 4)?.try_into().ok()?))
    }

    fn read_u64(buffer: &[u8], at: usize) -> Option<u64> {
        Some(u64::from_ne_bytes(buffer.get(at..at + 8)?.try_into().ok()?))
    }

    pub(super) fn read_entries(dir: &Path) -> std::io::Result<Vec<RawEntry>> {
        let path = CString::new(dir.as_os_str().as_bytes())
            .map_err(|_| std::io::Error::from(std::io::ErrorKind::InvalidInput))?;
        let fd = unsafe {
            libc::open(
                path.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        struct Fd(libc::c_int);
        impl Drop for Fd {
            fn drop(&mut self) {
                unsafe { libc::close(self.0) };
            }
        }
        let fd = Fd(fd);
        let mut attributes = libc::attrlist {
            bitmapcount: libc::ATTR_BIT_MAP_COUNT,
            reserved: 0,
            commonattr: COMMON,
            volattr: 0,
            dirattr: 0,
            fileattr: FILE,
            forkattr: 0,
        };
        let mut buffer = vec![0_u8; 64 * 1024];
        let mut entries = Vec::new();
        loop {
            let count = unsafe {
                libc::getattrlistbulk(
                    fd.0,
                    (&mut attributes as *mut libc::attrlist).cast(),
                    buffer.as_mut_ptr().cast(),
                    buffer.len(),
                    libc::FSOPT_PACK_INVAL_ATTRS as u64,
                )
            };
            if count < 0 {
                return Err(std::io::Error::last_os_error());
            }
            if count == 0 {
                return Ok(entries);
            }
            let mut offset = 0_usize;
            for _ in 0..count {
                let malformed = || std::io::Error::from(std::io::ErrorKind::InvalidData);
                let length = read_u32(&buffer, offset).ok_or_else(malformed)? as usize;
                let entry = buffer.get(offset..offset + length).ok_or_else(malformed)?;
                entries.push(parse_entry(entry).ok_or_else(malformed)?);
                offset += length;
            }
        }
    }

    /// One packed entry: length, returned-attribute set, then the requested
    /// attributes in bit order (FSOPT_PACK_INVAL_ATTRS keeps invalid ones as
    /// zero-filled placeholders, so the layout is fixed).
    fn parse_entry(entry: &[u8]) -> Option<RawEntry> {
        let mut at = 4;
        let returned_common = read_u32(entry, at)?;
        let returned_file = read_u32(entry, at + 12)?;
        at += std::mem::size_of::<libc::attribute_set_t>();
        // attrreference_t: offset from its own position, then length.
        let name_offset = i32::from_ne_bytes(entry.get(at..at + 4)?.try_into().ok()?);
        let name_length = read_u32(entry, at + 4)? as usize;
        let name_start = usize::try_from(at as i64 + name_offset as i64).ok()?;
        let name_bytes = entry.get(name_start..name_start + name_length)?;
        let name_bytes = name_bytes.strip_suffix(&[0]).unwrap_or(name_bytes);
        let name = OsString::from_vec(name_bytes.to_vec());
        at += 8;
        let objtype = read_u32(entry, at)?;
        at += 4;
        let seconds = read_u64(entry, at)? as i64;
        let nanos = read_u64(entry, at + 8)? as i64;
        at += 16;
        let inode = read_u64(entry, at)?;
        at += 8;
        let length = read_u64(entry, at)?;
        let has = |mask: libc::attrgroup_t| returned_common & mask == mask;
        let complete = has(libc::ATTR_CMN_OBJTYPE | libc::ATTR_CMN_MODTIME | libc::ATTR_CMN_FILEID)
            && (objtype != VREG || returned_file & FILE == FILE);
        let modified = (seconds >= 0 && (0..1_000_000_000).contains(&nanos))
            .then(|| SystemTime::UNIX_EPOCH + Duration::new(seconds as u64, nanos as u32));
        let stat = complete.then_some(RawStat {
            // A symlink's own size is not reported in the data fork; lstat
            // gives the target length, which a symlink's stamp never needs:
            // a symlink is never at rest.
            len: if objtype == VREG { length } else { 0 },
            inode,
            modified,
            is_symlink: objtype == VLNK,
        });
        Some(RawEntry { name, stat })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn owned(values: Arc<Vec<Arc<String>>>) -> Vec<String> {
        values.iter().map(|value| String::clone(value)).collect()
    }

    fn parse(_path: &Path, bytes: std::io::Result<Vec<u8>>) -> Option<String> {
        String::from_utf8(bytes.ok()?).ok()
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn bulk_listing_matches_read_dir_and_stat() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("a.json"), "{}").unwrap();
        age(&dir.path().join("a.json"), 60);
        std::os::unix::fs::symlink(dir.path().join("a.json"), dir.path().join("link.json"))
            .unwrap();
        fs::create_dir(dir.path().join("dir.json")).unwrap();
        fs::write(dir.path().join("note.txt"), "x").unwrap();
        // More entries than one bulk call tends to return.
        for index in 0..600 {
            fs::write(dir.path().join(format!("claim-{index:04}.json")), "{}").unwrap();
        }
        let mut bulk: Vec<RawEntry> = bulk::read_entries(dir.path())
            .unwrap()
            .into_iter()
            .filter(|entry| Path::new(&entry.name).extension() == Some("json".as_ref()))
            .collect();
        let mut portable = read_entries_portable(dir.path()).unwrap();
        bulk.sort_by(|left, right| left.name.cmp(&right.name));
        portable.sort_by(|left, right| left.name.cmp(&right.name));
        assert_eq!(bulk.len(), portable.len());
        for (bulk, portable) in bulk.iter().zip(&portable) {
            assert_eq!(bulk.name, portable.name);
            let (bulk, portable) = (bulk.stat.as_ref().unwrap(), portable.stat.as_ref().unwrap());
            assert_eq!(bulk.inode, portable.inode, "{:?}", portable.modified);
            assert_eq!(bulk.modified, portable.modified);
            assert_eq!(bulk.is_symlink, portable.is_symlink);
            if !bulk.is_symlink && bulk.len != 0 {
                assert_eq!(bulk.len, portable.len);
            }
        }
        // The regular file's size is reported.
        let a = bulk.iter().find(|entry| entry.name == "a.json").unwrap();
        assert_eq!(a.stat.as_ref().unwrap().len, 2);
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
    fn only_the_file_that_changed_is_read_again() {
        let dir = tempfile::tempdir().unwrap();
        for name in ["a.json", "b.json", "c.json"] {
            fs::write(dir.path().join(name), name).unwrap();
            age(&dir.path().join(name), 60);
        }
        let read = |dir: &Path| owned(parsed_json_dir(dir, parse, Ord::cmp).unwrap());
        assert_eq!(read(dir.path()), ["a.json", "b.json", "c.json"]);
        let before = files_parsed();

        // A live writer keeps one file fresh: it is read every time, the
        // settled ones are not, and the answer is always current.
        let b = dir.path().join("b.json");
        fs::write(&b, "b-1").unwrap();
        assert_eq!(read(dir.path()), ["a.json", "b-1", "c.json"]);
        fs::write(&b, "b-2").unwrap();
        assert_eq!(read(dir.path()), ["a.json", "b-2", "c.json"]);
        assert_eq!(files_parsed() - before, 2, "settled files were re-read");

        // Once it rests, its parse is kept too.
        age(&b, 60);
        assert_eq!(read(dir.path()), ["a.json", "b-2", "c.json"]);
        let rested = files_parsed();
        assert_eq!(read(dir.path()), ["a.json", "b-2", "c.json"]);
        assert_eq!(files_parsed(), rested);
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
            |dir: &Path| -> Vec<String> { owned(parsed_json_dir(dir, parse, Ord::cmp).unwrap()) };
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
            owned(parsed_json_dir(&claims, parse, Ord::cmp).unwrap()),
            ["one"]
        );
        fs::write(&target, "two").unwrap();
        age(&target, 30);
        assert_eq!(
            owned(parsed_json_dir(&claims, parse, Ord::cmp).unwrap()),
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
        let ascending = |dir: &Path| owned(parsed_json_dir(dir, parse, Ord::cmp).unwrap());
        let descending = |dir: &Path| {
            owned(
                parsed_json_dir(dir, parse, |left: &String, right: &String| right.cmp(left))
                    .unwrap(),
            )
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
            owned(parsed_json_dir(dir.path(), parse, Ord::cmp).unwrap()),
            ["one"]
        );
        assert_eq!(
            unread_files(),
            unread + 2,
            "a file that was read was counted"
        );
    }

    #[cfg(unix)]
    #[test]
    fn watched_dir_reuses_snapshot_until_notified_and_invalidates_immediately() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("a.json");
        fs::write(&file, "one").unwrap();
        age(&file, 60);
        // Age the directory mtime using utimensat so dir_rested_stamp considers it at rest.
        let c_dir = std::ffi::CString::new(dir.path().as_os_str().as_encoded_bytes()).unwrap();
        let old = SystemTime::now() - Duration::from_secs(60);
        let secs = old
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs() as libc::time_t;
        let ts = libc::timespec {
            tv_sec: secs,
            tv_nsec: 0,
        };
        let rc = unsafe { libc::utimensat(libc::AT_FDCWD, c_dir.as_ptr(), [ts, ts].as_ptr(), 0) };
        assert_eq!(rc, 0, "could not age temp directory");

        register_watched_dirs(&[dir.path().to_path_buf()]);

        let first = parsed_json_dir(dir.path(), parse, Ord::cmp).unwrap();
        assert_eq!(owned(first.clone()), ["one"]);
        assert_eq!(files_parsed(), 1);

        // Second call returns the exact Arc without re-parsing or re-listing.
        let second = parsed_json_dir(dir.path(), parse, Ord::cmp).unwrap();
        assert!(Arc::ptr_eq(&first, &second));
        assert_eq!(files_parsed(), 1);

        // After an in-place change + watcher notification, the new content is seen immediately.
        fs::write(&file, "two").unwrap();
        notify_watched_dir_changed(&file);
        let third = parsed_json_dir(dir.path(), parse, Ord::cmp).unwrap();
        assert_eq!(owned(third), ["two"]);
        assert_eq!(files_parsed(), 2);

        // Notifying an unrelated watched root must not invalidate `dir`'s counter.
        let other_dir = tempfile::tempdir().unwrap();
        register_watched_dirs(&[other_dir.path().to_path_buf()]);
        let gen_before = watched_root_counter(dir.path())
            .unwrap()
            .load(Ordering::Acquire);
        notify_watched_dir_changed(&other_dir.path().join("other.json"));
        let gen_after = watched_root_counter(dir.path())
            .unwrap()
            .load(Ordering::Acquire);
        assert_eq!(
            gen_before, gen_after,
            "change in another watched root must not bump this root's watch generation"
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
