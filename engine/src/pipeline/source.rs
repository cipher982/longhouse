//! What the parser reads, made explicit.
//!
//! The parser turns a provider transcript into Longhouse events. It used to
//! open the transcript file itself and reach beside it for whatever else it
//! needed: a subagent sidecar, Cursor's store, git metadata, the Pi/OMP blob
//! store. That made its output a function of one laptop's disk, so a host that
//! holds only the shipped raw bytes could not reproduce it.
//!
//! Two inputs replace the path:
//!
//! - [`SourceBytes`]: the transcript itself, by byte range. On a laptop it is
//!   the file ([`FileSource`]); on a host it is the stored raw records
//!   ([`MemorySource`]). Both answer the same reads with the same bytes.
//! - [`Witness`]: every input that is not in the transcript bytes. Each method
//!   is one such input. [`FileWitness`] answers from the laptop disk exactly as
//!   the parser used to; [`NoWitness`] answers nothing. A witness method a host
//!   cannot answer from shipped data is a capture gap by construction, and
//!   [`RecordingWitness`] lists which ones a parse actually consulted.
//!
//! See `docs/specs/host-normalized-transcripts.md` (control-plane), Phase 1.

use std::cell::RefCell;
use std::collections::BTreeSet;
use std::io::{Cursor, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};

/// A transcript's bytes, addressed by offset.
pub trait SourceBytes {
    /// The path the transcript was captured from. Used only for identity the
    /// parser derives from the location (file stem, workflow run directory,
    /// Antigravity brain id); never opened through this trait's caller.
    fn identity_path(&self) -> &Path;
    /// Current length in bytes.
    fn len(&self) -> Result<u64>;
    /// A reader over `[start, end)`, clamped to the current length.
    fn reader(&self, start: u64, end: u64) -> Result<Box<dyn Read + '_>>;
    /// The bytes in `[start, end)`, clamped to the current length.
    fn read_range(&self, start: u64, end: u64) -> Result<Vec<u8>> {
        let mut out = Vec::new();
        self.reader(start, end)?.read_to_end(&mut out)?;
        Ok(out)
    }
    /// The OMP session header at the start of the transcript.
    fn omp_header(&self) -> Result<crate::omp_session::OmpSessionHeader> {
        crate::omp_session::read_session_header_from(
            self.reader(0, u64::MAX)?,
            self.identity_path(),
        )
    }
    /// The whole transcript as text (full-document formats).
    fn read_to_string(&self) -> Result<String> {
        let bytes = self.read_range(0, self.len()?)?;
        String::from_utf8(bytes).context("transcript is not UTF-8")
    }
}

/// The transcript file on this machine.
pub struct FileSource<'a> {
    path: &'a Path,
}

impl<'a> FileSource<'a> {
    pub fn new(path: &'a Path) -> Self {
        Self { path }
    }
}

impl SourceBytes for FileSource<'_> {
    fn identity_path(&self) -> &Path {
        self.path
    }

    fn len(&self) -> Result<u64> {
        Ok(std::fs::metadata(self.path)
            .with_context(|| format!("Failed to stat {}", self.path.display()))?
            .len())
    }

    fn reader(&self, start: u64, end: u64) -> Result<Box<dyn Read + '_>> {
        let mut file = std::fs::File::open(self.path)
            .with_context(|| format!("Failed to open {}", self.path.display()))?;
        let len = file
            .metadata()
            .with_context(|| format!("Failed to stat {}", self.path.display()))?
            .len();
        let end = end.min(len);
        let start = start.min(end);
        file.seek(SeekFrom::Start(start))
            .with_context(|| format!("Failed to seek {}", self.path.display()))?;
        Ok(Box::new(file.take(end - start)))
    }

    fn omp_header(&self) -> Result<crate::omp_session::OmpSessionHeader> {
        // The cached file read: header lookups run on every transcript write.
        crate::omp_session::read_session_header(self.path)
    }
}

/// Transcript bytes held in memory: a host's stored raw records, concatenated
/// in source order from offset 0.
pub struct MemorySource {
    identity_path: PathBuf,
    bytes: Vec<u8>,
}

impl MemorySource {
    pub fn new(identity_path: impl Into<PathBuf>, bytes: Vec<u8>) -> Self {
        Self {
            identity_path: identity_path.into(),
            bytes,
        }
    }
}

impl SourceBytes for MemorySource {
    fn identity_path(&self) -> &Path {
        &self.identity_path
    }

    fn len(&self) -> Result<u64> {
        Ok(self.bytes.len() as u64)
    }

    fn reader(&self, start: u64, end: u64) -> Result<Box<dyn Read + '_>> {
        let len = self.bytes.len() as u64;
        let end = end.min(len) as usize;
        let start = (start as usize).min(end);
        Ok(Box::new(Cursor::new(&self.bytes[start..end])))
    }
}

/// Workspace facts for a session whose transcript does not name its cwd.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkspaceWitness {
    pub cwd: String,
    pub project: Option<String>,
    pub git_repo: Option<String>,
}

/// Every input the parser reads from outside the transcript bytes.
pub trait Witness {
    /// Claude Task/Agent subagent `agent-<id>.meta.json` sidecar text.
    fn subagent_meta(&self) -> Option<String>;
    /// Cursor agent-transcript `(started_at, last_activity_at)` from its store.
    fn cursor_transcript_timestamps(&self) -> Option<(DateTime<Utc>, DateTime<Utc>)>;
    /// Workspace for an Antigravity or Cursor session with no cwd in its transcript.
    fn workspace(&self) -> Option<WorkspaceWitness>;
    /// Git `(project, repo)` resolved from a working directory on disk.
    fn git_info(&self, cwd: &str) -> (Option<String>, Option<String>);
    /// Pi/OMP blob store root, for pasted-image capture.
    fn blob_root(&self) -> Option<PathBuf>;
    /// Time for a record that carries no clock of its own.
    fn fallback_time(&self) -> DateTime<Utc>;
}

/// Answers every witness question from this machine's disk, as the parser
/// always has.
pub struct FileWitness<'a> {
    path: &'a Path,
}

impl<'a> FileWitness<'a> {
    pub fn new(path: &'a Path) -> Self {
        Self { path }
    }
}

impl Witness for FileWitness<'_> {
    fn subagent_meta(&self) -> Option<String> {
        const MAX_SIDECAR_BYTES: u64 = 64 * 1024;
        let stem = self.path.file_stem().and_then(|value| value.to_str())?;
        if !stem.starts_with("agent-") {
            return None;
        }
        let sidecar = self.path.with_file_name(format!("{stem}.meta.json"));
        if std::fs::metadata(&sidecar).ok()?.len() > MAX_SIDECAR_BYTES {
            return None;
        }
        std::fs::read_to_string(&sidecar).ok()
    }

    fn cursor_transcript_timestamps(&self) -> Option<(DateTime<Utc>, DateTime<Utc>)> {
        crate::cursor_store::cursor_transcript_timestamps(self.path)
    }

    fn workspace(&self) -> Option<WorkspaceWitness> {
        if let Some(conversation_id) = super::parser::antigravity_session_id_from_path(self.path) {
            let workspace =
                crate::antigravity_workspace::antigravity_workspace(self.path, &conversation_id)?;
            return Some(WorkspaceWitness {
                cwd: workspace.cwd,
                project: None,
                git_repo: workspace.git_repo,
            });
        }
        let conversation_id = super::parser::cursor_session_id_from_path(self.path)?;
        let store = crate::cursor_visibility::configured_cursor_store(&conversation_id)?;
        let (cwd, project, git_repo) = crate::cursor_store::cursor_workspace_facts(&store)?;
        Some(WorkspaceWitness {
            cwd,
            project,
            git_repo,
        })
    }

    fn git_info(&self, cwd: &str) -> (Option<String>, Option<String>) {
        super::parser::resolve_git_info(Path::new(cwd))
    }

    fn blob_root(&self) -> Option<PathBuf> {
        crate::media_redaction::provider_blob_root(self.path)
    }

    fn fallback_time(&self) -> DateTime<Utc> {
        // The file's modification time: stable across re-parses of the same
        // bytes, and close to when the clockless record was written.
        std::fs::metadata(self.path)
            .and_then(|meta| meta.modified())
            .map(DateTime::<Utc>::from)
            .unwrap_or(DateTime::<Utc>::UNIX_EPOCH)
    }
}

/// Answers nothing: what a host holding only the transcript bytes knows.
pub struct NoWitness;

impl Witness for NoWitness {
    fn subagent_meta(&self) -> Option<String> {
        None
    }
    fn cursor_transcript_timestamps(&self) -> Option<(DateTime<Utc>, DateTime<Utc>)> {
        None
    }
    fn workspace(&self) -> Option<WorkspaceWitness> {
        None
    }
    fn git_info(&self, _cwd: &str) -> (Option<String>, Option<String>) {
        (None, None)
    }
    fn blob_root(&self) -> Option<PathBuf> {
        None
    }
    fn fallback_time(&self) -> DateTime<Utc> {
        DateTime::<Utc>::UNIX_EPOCH
    }
}

/// Wraps a witness and records which questions returned something.
pub struct RecordingWitness<W> {
    inner: W,
    answered: RefCell<BTreeSet<&'static str>>,
}

impl<W: Witness> RecordingWitness<W> {
    pub fn new(inner: W) -> Self {
        Self {
            inner,
            answered: RefCell::new(BTreeSet::new()),
        }
    }

    /// Names of the witness inputs that contributed an answer.
    pub fn answered(&self) -> Vec<&'static str> {
        self.answered.borrow().iter().copied().collect()
    }

    fn note(&self, name: &'static str, answered: bool) {
        if answered {
            self.answered.borrow_mut().insert(name);
        }
    }
}

impl<W: Witness> Witness for RecordingWitness<W> {
    fn subagent_meta(&self) -> Option<String> {
        let value = self.inner.subagent_meta();
        self.note("subagent_meta", value.is_some());
        value
    }
    fn cursor_transcript_timestamps(&self) -> Option<(DateTime<Utc>, DateTime<Utc>)> {
        let value = self.inner.cursor_transcript_timestamps();
        self.note("cursor_transcript_timestamps", value.is_some());
        value
    }
    fn workspace(&self) -> Option<WorkspaceWitness> {
        let value = self.inner.workspace();
        self.note("workspace", value.is_some());
        value
    }
    fn git_info(&self, cwd: &str) -> (Option<String>, Option<String>) {
        let value = self.inner.git_info(cwd);
        self.note("git_info", value.0.is_some() || value.1.is_some());
        value
    }
    fn blob_root(&self) -> Option<PathBuf> {
        let value = self.inner.blob_root();
        self.note("blob_root", value.is_some());
        value
    }
    fn fallback_time(&self) -> DateTime<Utc> {
        // Asked on every parse; whether it mattered shows up as a difference
        // the test attributes to it.
        self.inner.fallback_time()
    }
}

#[cfg(test)]
mod equivalence {
    //! Phase 1 check: the parser's output is a function of the transcript
    //! bytes plus the witness. For every fixture, parsing the file with the
    //! laptop witness must equal parsing the same bytes from memory with no
    //! witness, except where the laptop witness answered a question; those
    //! answers are the out-of-band inputs, recorded in
    //! `tests/fixtures/out_of_band_inputs.json`. Regenerate with
    //! `UPDATE_GOLDENS=1`.

    use super::*;
    use crate::pipeline::parser::{parse_source, ParseResult};
    use serde_json::{json, Value};
    use std::collections::BTreeMap;

    fn fixtures_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
    }

    /// Transcript fixtures the file parser reads (OpenCode and Cursor store
    /// sources have their own extractors and are covered in Phase 2).
    fn transcript_fixtures() -> Vec<(PathBuf, Option<&'static str>)> {
        let root = fixtures_root();
        let mut out = Vec::new();
        for dir in [
            "golden/claude",
            "golden/codex",
            "golden/cursor",
            "golden/antigravity",
            "golden/antigravity_legacy_json",
            "golden/pi",
            "golden/omp",
            "adversarial/claude",
            "adversarial/codex",
            "adversarial/antigravity_legacy_json",
            "codex",
        ] {
            let provider = if dir.ends_with("/omp") {
                Some("omp")
            } else if dir.ends_with("/pi") {
                Some("pi")
            } else {
                None
            };
            let Ok(entries) = std::fs::read_dir(root.join(dir)) else {
                continue;
            };
            let mut paths: Vec<PathBuf> =
                entries.filter_map(|e| e.ok().map(|e| e.path())).collect();
            paths.sort();
            for path in paths {
                let name = path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or_default();
                if (name.ends_with(".jsonl") || name.ends_with(".json"))
                    && !name.contains(".expected.")
                {
                    out.push((path, provider));
                }
            }
        }
        for entry in walk(&root.join("workflows")) {
            if entry.extension().and_then(|e| e.to_str()) == Some("jsonl") {
                out.push((entry, None));
            }
        }
        out
    }

    fn walk(dir: &Path) -> Vec<PathBuf> {
        let mut out = Vec::new();
        let Ok(entries) = std::fs::read_dir(dir) else {
            return out;
        };
        let mut paths: Vec<PathBuf> = entries.filter_map(|e| e.ok().map(|e| e.path())).collect();
        paths.sort();
        for path in paths {
            if path.is_dir() {
                out.extend(walk(&path));
            } else {
                out.push(path);
            }
        }
        out
    }

    fn snapshot(result: &ParseResult) -> Value {
        json!({
            "events": result.events,
            "source_lines": result.source_lines,
            "media_objects": result.media_objects,
            "provider_facts": result.provider_facts,
            "metadata": result.metadata,
            "last_good_offset": result.last_good_offset,
            "candidate_records": result.candidate_records,
        })
    }

    /// Top-level and metadata keys whose values differ.
    fn differing_keys(a: &Value, b: &Value) -> Vec<String> {
        let mut out = Vec::new();
        for (key, left) in a.as_object().into_iter().flatten() {
            let right = &b[key.as_str()];
            if left == right {
                continue;
            }
            if key == "metadata" {
                for (field, value) in left.as_object().into_iter().flatten() {
                    if value != &right[field.as_str()] {
                        out.push(format!("metadata.{field}"));
                    }
                }
            } else {
                out.push(key.clone());
            }
        }
        out
    }

    struct ClockOnly(DateTime<Utc>);

    impl Witness for ClockOnly {
        fn subagent_meta(&self) -> Option<String> {
            None
        }
        fn cursor_transcript_timestamps(&self) -> Option<(DateTime<Utc>, DateTime<Utc>)> {
            None
        }
        fn workspace(&self) -> Option<WorkspaceWitness> {
            None
        }
        fn git_info(&self, _cwd: &str) -> (Option<String>, Option<String>) {
            (None, None)
        }
        fn blob_root(&self) -> Option<PathBuf> {
            None
        }
        fn fallback_time(&self) -> DateTime<Utc> {
            self.0
        }
    }

    fn second_line_offset(bytes: &[u8]) -> Option<u64> {
        let first = bytes.iter().position(|&b| b == b'\n')? as u64 + 1;
        (first < bytes.len() as u64).then_some(first)
    }

    #[test]
    fn parser_output_is_transcript_bytes_plus_witness() {
        let root = fixtures_root();
        let mut inventory: BTreeMap<String, Value> = BTreeMap::new();
        let mut unexplained = Vec::new();
        for (path, provider) in transcript_fixtures() {
            let bytes = std::fs::read(&path).unwrap();
            let is_json_document = path.extension().and_then(|e| e.to_str()) == Some("json");
            let mut offsets = vec![0u64];
            if !is_json_document {
                offsets.extend(second_line_offset(&bytes));
            }
            for offset in offsets {
                let witness = RecordingWitness::new(FileWitness::new(&path));
                let from_file =
                    parse_source(&FileSource::new(&path), &witness, offset, None, provider);
                // Same clock as the file parse, nothing else: isolates the
                // clock from the other witness inputs.
                let clock = ClockOnly(FileWitness::new(&path).fallback_time());
                let from_memory = parse_source(
                    &MemorySource::new(&path, bytes.clone()),
                    &clock,
                    offset,
                    None,
                    provider,
                );
                // The host receives a hash of the path, not the path. Keep
                // only the extension (the format dispatch) and see what else
                // the location was supplying.
                let neutral_path = PathBuf::from(format!(
                    "source.{}",
                    path.extension().and_then(|e| e.to_str()).unwrap_or("jsonl")
                ));
                let pathless = parse_source(
                    &MemorySource::new(&neutral_path, bytes.clone()),
                    &clock,
                    offset,
                    None,
                    provider,
                );
                let clockless = parse_source(
                    &MemorySource::new(&path, bytes.clone()),
                    &NoWitness,
                    offset,
                    None,
                    provider,
                );
                let key = format!(
                    "{}@{}",
                    path.strip_prefix(&root).unwrap().display(),
                    if offset == 0 { "start" } else { "resume" }
                );
                match (from_file, from_memory) {
                    (Ok(file), Ok(memory)) => {
                        let differing = differing_keys(&snapshot(&file), &snapshot(&memory));
                        let mut answered = witness.answered();
                        if clockless
                            .as_ref()
                            .is_ok_and(|bare| snapshot(bare) != snapshot(&memory))
                        {
                            answered.push("fallback_time");
                        }
                        if pathless
                            .as_ref()
                            .map_or(true, |bare| snapshot(bare) != snapshot(&memory))
                        {
                            answered.push("source_path");
                        }
                        if !differing.is_empty() && answered.is_empty() {
                            unexplained.push(format!("{key}: {differing:?}"));
                        }
                        // Which inputs, not which fields: what git resolves
                        // to depends on the machine running the test.
                        if !answered.is_empty() {
                            inventory.insert(key, json!(answered));
                        }
                    }
                    (Err(file), Err(memory)) => {
                        assert_eq!(file.to_string(), memory.to_string(), "{key}");
                    }
                    (file, memory) => {
                        unexplained.push(format!(
                            "{key}: file ok={} memory ok={}",
                            file.is_ok(),
                            memory.is_ok()
                        ));
                    }
                }
            }
        }
        assert!(
            unexplained.is_empty(),
            "output differs without any witness input: {unexplained:#?}"
        );
        let expected_path = root.join("out_of_band_inputs.json");
        let actual = serde_json::to_string_pretty(&inventory).unwrap() + "\n";
        if std::env::var_os("UPDATE_GOLDENS").is_some() {
            std::fs::write(&expected_path, &actual).unwrap();
            return;
        }
        let expected = std::fs::read_to_string(&expected_path).unwrap_or_default();
        assert_eq!(
            expected, actual,
            "out-of-band inputs changed; rerun with UPDATE_GOLDENS=1 and review"
        );
    }
}
