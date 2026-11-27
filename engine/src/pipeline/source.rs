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
use std::path::Path;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};

/// Whether the source is a whole JSON document or newline-delimited records.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceFormat {
    Jsonl,
    JsonDocument,
}

/// What the transcript's location tells the parser, as plain values. A laptop
/// computes it from the path ([`SourceIdentity::from_path`]); it is shipped
/// beside the raw bytes, so a host that only has a hash of the path parses
/// identically.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct SourceIdentity {
    pub format: SourceFormat,
    /// The session id the location implies, before the content can say
    /// otherwise: an Antigravity brain id, a UUID file stem, or a UUID derived
    /// from the full path.
    pub location_session_id: String,
    /// Claude dynamic-workflow run directory (`subagents/workflows/<run>`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workflow_run_id: Option<String>,
    /// A Codex `rollout-*.jsonl` file.
    #[serde(default)]
    pub codex_rollout: bool,
    /// For log lines only; never parsed, and never shipped: the host knows a
    /// source only by its opaque id.
    #[serde(skip)]
    pub label: String,
}

impl SourceIdentity {
    pub fn from_path(path: &Path) -> Self {
        let format = if path
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| e.eq_ignore_ascii_case("json"))
        {
            SourceFormat::JsonDocument
        } else {
            SourceFormat::Jsonl
        };
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("unknown");
        let location_session_id =
            if let Some(id) = super::parser::antigravity_session_id_from_path(path) {
                id
            } else if uuid::Uuid::parse_str(stem).is_ok() {
                stem.to_string()
            } else {
                uuid::Uuid::new_v5(
                    &uuid::Uuid::NAMESPACE_URL,
                    path.to_string_lossy().as_bytes(),
                )
                .to_string()
            };
        Self {
            format,
            location_session_id,
            workflow_run_id: super::parser::workflow_run_id_from_path(path),
            codex_rollout: super::parser::is_codex_rollout_path(path),
            label: path.display().to_string(),
        }
    }
}

/// A transcript's bytes, addressed by offset.
pub trait SourceBytes {
    /// What the transcript's location tells the parser.
    fn identity(&self) -> &SourceIdentity;
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
            Path::new(&self.identity().label),
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
    identity: SourceIdentity,
}

impl<'a> FileSource<'a> {
    pub fn new(path: &'a Path) -> Self {
        Self::named(path, path)
    }

    /// Bytes read from `path`, identified as the source at `identity_path`: a
    /// compressed Codex rollout is read from its decompressed cache copy but
    /// is still the rollout it names.
    pub fn named(path: &'a Path, identity_path: &Path) -> Self {
        Self {
            path,
            identity: SourceIdentity::from_path(identity_path),
        }
    }
}

impl SourceBytes for FileSource<'_> {
    fn identity(&self) -> &SourceIdentity {
        &self.identity
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
pub struct MemorySource<'a> {
    identity: SourceIdentity,
    /// Source offset of `bytes[0]`. A host holds a source from the first
    /// position the laptop shipped, which is not always 0: Claude startup
    /// metadata before the first event is acknowledged locally and never sent.
    base: u64,
    bytes: std::borrow::Cow<'a, [u8]>,
}

impl MemorySource<'static> {
    pub fn new(identity: SourceIdentity, bytes: Vec<u8>) -> Self {
        Self::at(identity, 0, bytes)
    }
}

impl<'a> MemorySource<'a> {
    /// Bytes that start at source offset `base`; reads before it see nothing.
    pub fn at(
        identity: SourceIdentity,
        base: u64,
        bytes: impl Into<std::borrow::Cow<'a, [u8]>>,
    ) -> Self {
        Self {
            identity,
            base,
            bytes: bytes.into(),
        }
    }
}

impl SourceBytes for MemorySource<'_> {
    fn identity(&self) -> &SourceIdentity {
        &self.identity
    }

    fn len(&self) -> Result<u64> {
        Ok(self.base + self.bytes.len() as u64)
    }

    fn reader(&self, start: u64, end: u64) -> Result<Box<dyn Read + '_>> {
        // A read that starts before the stored bytes asks for bytes the host
        // never received; it sees nothing rather than the stored bytes shifted.
        if start < self.base {
            return Ok(Box::new(Cursor::new(&self.bytes[..0])));
        }
        let len = self.bytes.len() as u64;
        let end = end.saturating_sub(self.base).min(len) as usize;
        let start = ((start - self.base) as usize).min(end);
        Ok(Box::new(Cursor::new(&self.bytes[start..end])))
    }
}

/// Workspace facts for a session whose transcript does not name its cwd.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
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
    /// The bytes a Pi/OMP `blob:sha256:` image reference names, if known.
    fn blob(&self, reference: &str, mime_type: &str) -> Option<Vec<u8>>;
    /// Time for a record that carries no clock of its own.
    fn fallback_time(&self) -> DateTime<Utc>;
    /// The parent an Antigravity conversation was spawned by, from the
    /// parent's `subagents/<child>.json` record. Only the laptop's disk and a
    /// captured witness can answer; everything else knows no parent.
    fn antigravity_child(&self) -> Option<AntigravityChild> {
        None
    }
}

pub use crate::antigravity_subagents::AntigravityChild;

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

    fn blob(&self, reference: &str, mime_type: &str) -> Option<Vec<u8>> {
        if !reference.starts_with("blob:sha256:") {
            // Not a Pi digest: an Antigravity `media` URI, a local file.
            return crate::media_redaction::antigravity_media_bytes(self.path, reference);
        }
        let root = crate::media_redaction::provider_blob_root(self.path)?;
        crate::media_redaction::provider_blob_bytes(&root, reference, mime_type)
    }

    fn fallback_time(&self) -> DateTime<Utc> {
        // The file's modification time: stable across re-parses of the same
        // bytes, and close to when the clockless record was written.
        std::fs::metadata(self.path)
            .and_then(|meta| meta.modified())
            .map(DateTime::<Utc>::from)
            .unwrap_or(DateTime::<Utc>::UNIX_EPOCH)
    }

    fn antigravity_child(&self) -> Option<AntigravityChild> {
        crate::antigravity_subagents::child_of(self.path)
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
    fn blob(&self, _reference: &str, _mime_type: &str) -> Option<Vec<u8>> {
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
    fn blob(&self, reference: &str, mime_type: &str) -> Option<Vec<u8>> {
        let value = self.inner.blob(reference, mime_type);
        self.note("blob", value.is_some());
        value
    }
    fn fallback_time(&self) -> DateTime<Utc> {
        // Asked on every parse; whether it mattered shows up as a difference
        // the test attributes to it.
        self.inner.fallback_time()
    }
    fn antigravity_child(&self) -> Option<AntigravityChild> {
        let value = self.inner.antigravity_child();
        self.note("antigravity_child", value.is_some());
        value
    }
}

/// Git resolution for one working directory, as the laptop saw it.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct GitWitness {
    pub cwd: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub git_repo: Option<String>,
}

/// Fact kind that carries a [`CapturedWitness`] beside an envelope's raw
/// records. Captured, not derived: a rebuild must never drop it.
pub const CAPTURE_WITNESS_FACT_KIND: &str = "capture.witness";

/// Everything one parse took from outside the transcript bytes, in a form a
/// host can store and answer from. Shipped as a [`CAPTURE_WITNESS_FACT_KIND`]
/// fact so the host parses the same bytes with the same answers.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct CapturedWitness {
    pub identity: SourceIdentity,
    pub fallback_time: DateTime<Utc>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub subagent_meta: Option<String>,
    /// The Antigravity parent's record of spawning this conversation.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub antigravity_child: Option<AntigravityChild>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cursor_transcript_timestamps: Option<(DateTime<Utc>, DateTime<Utc>)>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace: Option<WorkspaceWitness>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub git: Vec<GitWitness>,
    /// The provider session this managed session ran before this source, when
    /// the source starts a new one (a `/clear` or a fresh launch under the
    /// same Longhouse session). The render opens with a reset boundary.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub previous_provider_session_id: Option<String>,
    /// The laptop's session binding names this very thread (a fork Longhouse
    /// started), which keeps a plain fork visible in the default timeline.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub managed_binding_is_exact: bool,
    /// The laptop shipped no render for this envelope (a Cursor agent
    /// transcript whose conversation `store.db` renders, or one with no clock
    /// and no events), so the host must not mint one either.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub render_withheld: bool,
    /// Content digest of each local media file a line named by URI (an
    /// Antigravity screenshot), as the laptop read it. The bytes ship as media
    /// objects; this is how the host finds them from the URI in the raw line.
    #[serde(default, skip_serializing_if = "std::collections::BTreeMap::is_empty")]
    pub media_files: std::collections::BTreeMap<String, String>,
    /// Blob bytes by sha256, from the shipped content-addressed media. Not
    /// serialized: media travels as media objects, not inside the fact.
    #[serde(skip)]
    pub media: std::collections::HashMap<String, Vec<u8>>,
}

impl Witness for CapturedWitness {
    fn subagent_meta(&self) -> Option<String> {
        self.subagent_meta.clone()
    }
    fn cursor_transcript_timestamps(&self) -> Option<(DateTime<Utc>, DateTime<Utc>)> {
        self.cursor_transcript_timestamps
    }
    fn workspace(&self) -> Option<WorkspaceWitness> {
        self.workspace.clone()
    }
    fn git_info(&self, cwd: &str) -> (Option<String>, Option<String>) {
        self.git
            .iter()
            .find(|entry| entry.cwd == cwd)
            .map(|entry| (entry.project.clone(), entry.git_repo.clone()))
            .unwrap_or((None, None))
    }
    fn blob(&self, reference: &str, _mime_type: &str) -> Option<Vec<u8>> {
        let digest = match reference.strip_prefix("blob:sha256:") {
            Some(digest) => digest.to_ascii_lowercase(),
            None => self.media_files.get(reference)?.clone(),
        };
        self.media.get(&digest).cloned()
    }
    fn fallback_time(&self) -> DateTime<Utc> {
        self.fallback_time
    }
    fn antigravity_child(&self) -> Option<AntigravityChild> {
        self.antigravity_child.clone()
    }
}

/// The laptop witness, with a fixed clock, recording every answer it gives so
/// the shipper can send exactly what the parse used.
pub struct CapturingWitness<'a> {
    inner: FileWitness<'a>,
    fallback_time: DateTime<Utc>,
    subagent_meta: RefCell<Option<String>>,
    antigravity_child: RefCell<Option<AntigravityChild>>,
    cursor_transcript_timestamps: RefCell<Option<(DateTime<Utc>, DateTime<Utc>)>>,
    workspace: RefCell<Option<WorkspaceWitness>>,
    git: RefCell<Vec<GitWitness>>,
    media_files: RefCell<std::collections::BTreeMap<String, String>>,
}

impl<'a> CapturingWitness<'a> {
    /// `fallback_time` is the source epoch's opening time, which is shipped
    /// with every envelope; the laptop and a host then date a clockless record
    /// the same way.
    pub fn new(path: &'a Path, fallback_time: DateTime<Utc>) -> Self {
        Self {
            inner: FileWitness::new(path),
            fallback_time,
            subagent_meta: RefCell::new(None),
            antigravity_child: RefCell::new(None),
            cursor_transcript_timestamps: RefCell::new(None),
            workspace: RefCell::new(None),
            git: RefCell::new(Vec::new()),
            media_files: RefCell::new(std::collections::BTreeMap::new()),
        }
    }

    pub fn finish(self, identity: SourceIdentity) -> CapturedWitness {
        CapturedWitness {
            identity,
            fallback_time: self.fallback_time,
            subagent_meta: self.subagent_meta.into_inner(),
            antigravity_child: self.antigravity_child.into_inner(),
            cursor_transcript_timestamps: self.cursor_transcript_timestamps.into_inner(),
            workspace: self.workspace.into_inner(),
            git: self.git.into_inner(),
            media_files: self.media_files.into_inner(),
            previous_provider_session_id: None,
            managed_binding_is_exact: false,
            render_withheld: false,
            media: std::collections::HashMap::new(),
        }
    }
}

impl Witness for CapturingWitness<'_> {
    fn subagent_meta(&self) -> Option<String> {
        let value = self.inner.subagent_meta();
        // Retain structured sidecar metadata fields so host rebuilds recover
        // agentType, description, model, spawnDepth, and toolUseId cleanly.
        *self.subagent_meta.borrow_mut() = value
            .as_deref()
            .and_then(super::parser::sanitize_subagent_meta_witness);
        value
    }
    fn cursor_transcript_timestamps(&self) -> Option<(DateTime<Utc>, DateTime<Utc>)> {
        let value = self.inner.cursor_transcript_timestamps();
        *self.cursor_transcript_timestamps.borrow_mut() = value;
        value
    }
    fn workspace(&self) -> Option<WorkspaceWitness> {
        let value = self.inner.workspace();
        *self.workspace.borrow_mut() = value.clone();
        value
    }
    fn git_info(&self, cwd: &str) -> (Option<String>, Option<String>) {
        let value = self.inner.git_info(cwd);
        let mut git = self.git.borrow_mut();
        if !git.iter().any(|entry| entry.cwd == cwd) {
            git.push(GitWitness {
                cwd: cwd.to_string(),
                project: value.0.clone(),
                git_repo: value.1.clone(),
            });
        }
        value
    }
    fn blob(&self, reference: &str, mime_type: &str) -> Option<Vec<u8>> {
        // Blob bytes ship as media objects; the host answers from those.
        let bytes = self.inner.blob(reference, mime_type);
        if let (false, Some(bytes)) = (reference.starts_with("blob:sha256:"), bytes.as_ref()) {
            self.media_files.borrow_mut().insert(
                reference.to_string(),
                format!("{:x}", <sha2::Sha256 as sha2::Digest>::digest(bytes)),
            );
        }
        bytes
    }
    fn fallback_time(&self) -> DateTime<Utc> {
        self.fallback_time
    }
    fn antigravity_child(&self) -> Option<AntigravityChild> {
        let value = self.inner.antigravity_child();
        *self.antigravity_child.borrow_mut() = value.clone();
        value
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

    use std::path::PathBuf;

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
        fn blob(&self, _reference: &str, _mime_type: &str) -> Option<Vec<u8>> {
            None
        }
        fn fallback_time(&self) -> DateTime<Utc> {
            self.0
        }
    }

    /// A Cursor transcript's only clock is its store's session anchor.
    struct CursorAnchor(DateTime<Utc>);

    impl Witness for CursorAnchor {
        fn subagent_meta(&self) -> Option<String> {
            None
        }
        fn cursor_transcript_timestamps(&self) -> Option<(DateTime<Utc>, DateTime<Utc>)> {
            Some((self.0, self.0))
        }
        fn workspace(&self) -> Option<WorkspaceWitness> {
            None
        }
        fn git_info(&self, _cwd: &str) -> (Option<String>, Option<String>) {
            (None, None)
        }
        fn blob(&self, _reference: &str, _mime_type: &str) -> Option<Vec<u8>> {
            None
        }
        fn fallback_time(&self) -> DateTime<Utc> {
            self.0
        }
    }

    /// Cursor's agent transcript: reasoning blocks are reasoning (redacted ones
    /// withheld), not assistant text; a Shell call's `description` is its
    /// intent; `turn_ended` ends the turn with its status.
    #[test]
    fn cursor_transcript_items() {
        use crate::pipeline::items::{EndReason, ItemFacts, Visibility};
        use crate::pipeline::parser::Role;

        let path = fixtures_root().join("golden/cursor/items.jsonl");
        let bytes = std::fs::read(&path).unwrap();
        let anchor = "2026-11-15T13:00:00Z".parse().unwrap();
        let parsed = parse_source(
            &MemorySource::new(SourceIdentity::from_path(&path), bytes),
            &CursorAnchor(anchor),
            0,
            None,
            Some("cursor"),
        )
        .unwrap();
        let items: Vec<&ItemFacts> = parsed.events.iter().map(|event| &event.item).collect();
        for expected in [
            ItemFacts::reasoning(Visibility::Full),
            ItemFacts::reasoning(Visibility::Withheld),
            ItemFacts::intent(Some("List the files")),
            ItemFacts::turn_end(EndReason::Completed),
            ItemFacts::turn_end(EndReason::Error),
        ] {
            assert!(items.contains(&&expected), "missing {expected:?}");
        }
        // A redacted block's payload reaches no row, of any role.
        assert!(!parsed.events.iter().any(|event| event
            .content_text
            .as_deref()
            .is_some_and(|text| text.contains("opaque"))));
        assert!(parsed
            .events
            .iter()
            .all(|event| event.role != Role::Assistant
                || event.content_text.as_deref() != Some("opaque")));
        // Every row orders from the anchor, the turn ends included.
        assert!(parsed.events.iter().all(|event| event.timestamp == anchor));
        crate::pipeline::items::assert_item_snapshot(
            "tests/fixtures/golden/cursor/items.jsonl",
            parsed
                .events
                .iter()
                .map(|event| {
                    serde_json::json!({
                        "role": event.role,
                        "content_text": event.content_text,
                        "tool_name": event.tool_name,
                        "item": event.item,
                    })
                })
                .collect(),
        );
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
                    &MemorySource::new(SourceIdentity::from_path(&path), bytes.clone()),
                    &clock,
                    offset,
                    None,
                    provider,
                );
                // The host receives a hash of the path plus the shipped
                // SourceIdentity, never the path: parse with that identity and
                // a label that names nothing, and see whether the location
                // still supplied anything.
                let shipped_identity = SourceIdentity {
                    label: "shipped".to_string(),
                    ..SourceIdentity::from_path(&path)
                };
                let pathless = parse_source(
                    &MemorySource::new(shipped_identity, bytes.clone()),
                    &clock,
                    offset,
                    None,
                    provider,
                );
                let clockless = parse_source(
                    &MemorySource::new(SourceIdentity::from_path(&path), bytes.clone()),
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
