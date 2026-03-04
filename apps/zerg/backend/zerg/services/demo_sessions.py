"""Demo agent session builders for DB seeding."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


def _lh(wall: str, exit_code: int, token_count: int, output: str, chunk: str = "a1b2c3") -> str:
    """Wrap output in the Longhouse execution metadata format."""
    return (
        f"Chunk ID: {chunk}\n"
        f"Wall time: {wall} seconds\n"
        f"Process exited with code {exit_code}\n"
        f"Original token count: {token_count}\n"
        f"Output:\n{output}"
    )


def build_demo_agent_sessions(now: datetime | None = None) -> list[SessionIngest]:
    """Return demo agent sessions for seeding a demo DB.

    10 sessions across 5 days, 4 projects, 3 providers.
    Mix of short (8-12), medium (15-18), and long (25-28) events.
    Includes parallel tool batches, MCP tools, and realistic outputs.
    """
    anchor = now or datetime.now(timezone.utc)

    # -------------------------------------------------------------------------
    # Session 1: Claude — Semantic search implementation (longhouse)
    # Long feature session: 27 events, parallel batches, MCP tools
    # -------------------------------------------------------------------------
    t1 = anchor - timedelta(days=4, hours=2)
    s1 = [
        EventIngest(
            role="user",
            content_text="Add semantic search to the sessions timeline. Users should be able to search by meaning, not just keywords.",
            timestamp=t1,
        ),
        EventIngest(
            role="assistant",
            content_text="I'll implement semantic search using text embeddings. Let me start by understanding the current search setup and what embedding infrastructure we have.",
            timestamp=t1 + timedelta(seconds=8),
        ),
        # Parallel research batch
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s1_01",
            tool_input_json={"command": "rg -n 'embedding\\|semantic\\|vector' apps/zerg/backend/zerg/ --include='*.py' -l"},
            timestamp=t1 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="Grep",
            tool_call_id="call_s1_02",
            tool_input_json={"pattern": "def.*search", "path": "apps/zerg/backend/zerg/services/"},
            timestamp=t1 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="mcp__longhouse__query_insights",
            tool_call_id="call_s1_03",
            tool_input_json={"project": "longhouse", "insight_type": "pattern"},
            timestamp=t1 + timedelta(minutes=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s1_01",
            tool_output_text=_lh(
                "0.312",
                0,
                89,
                "apps/zerg/backend/zerg/services/agents_store.py\napps/zerg/backend/zerg/services/embedding_cache.py\napps/zerg/backend/zerg/models/agents.py",
            ),
            timestamp=t1 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s1_02",
            tool_output_text="agents_store.py:214:async def search_sessions(query: str, ...) -> list[Session]:\nagents_store.py:248:def _fts_search(query: str, ...)",
            timestamp=t1 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s1_03",
            tool_output_text='{"insights": [{"title": "FTS5 search uses BM25 ranking", "description": "Events indexed via virtual table triggers on insert."}]}',
            timestamp=t1 + timedelta(minutes=1, seconds=3),
        ),
        EventIngest(
            role="assistant",
            content_text="Good. We have `embedding_cache.py` already, and FTS5 search exists. I'll build semantic search on top: generate embeddings via OpenAI text-embedding-3-small, store as BLOB in a new column, use cosine similarity for retrieval.",
            timestamp=t1 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            tool_name="Read",
            tool_input_json={"file_path": "apps/zerg/backend/zerg/services/embedding_cache.py"},
            timestamp=t1 + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_output_text='"""Embedding cache — generates and caches text embeddings.\n\nUses OpenAI text-embedding-3-small (256 dimensions) by default.\nFalls back gracefully when no API key configured.\n"""\n\nasync def get_embedding(text: str) -> list[float] | None:\n    ...\n\nasync def batch_embed(texts: list[str]) -> list[list[float] | None]:\n    ...',
            timestamp=t1 + timedelta(minutes=3, seconds=8),
        ),
        EventIngest(
            role="assistant",
            content_text="The cache layer is clean. Now adding a `session_embedding` column and similarity search to `agents_store.py`.",
            timestamp=t1 + timedelta(minutes=4),
        ),
        EventIngest(
            role="assistant",
            tool_name="Read",
            tool_input_json={"file_path": "apps/zerg/backend/zerg/services/agents_store.py", "offset": 210, "limit": 60},
            timestamp=t1 + timedelta(minutes=5),
        ),
        EventIngest(
            role="tool",
            tool_output_text="async def search_sessions(\n    query: str,\n    days_back: int = 14,\n    limit: int = 20,\n    project: str | None = None,\n) -> SessionSearchResult:\n    fts_results = await _fts_search(query, days_back, limit, project)\n    return SessionSearchResult(sessions=fts_results, mode='fts')",
            timestamp=t1 + timedelta(minutes=5, seconds=6),
        ),
        EventIngest(
            role="assistant",
            tool_name="Edit",
            tool_input_json={
                "file_path": "apps/zerg/backend/zerg/services/agents_store.py",
                "old_string": "    fts_results = await _fts_search(query, days_back, limit, project)\n    return SessionSearchResult(sessions=fts_results, mode='fts')",
                "new_string": "    if use_semantic and (emb := await get_embedding(query)):\n        semantic = await _cosine_search(emb, days_back, limit, project)\n        return SessionSearchResult(sessions=semantic, mode='semantic')\n    fts_results = await _fts_search(query, days_back, limit, project)\n    return SessionSearchResult(sessions=fts_results, mode='fts')",
            },
            timestamp=t1 + timedelta(minutes=7),
        ),
        EventIngest(role="tool", tool_output_text="File updated successfully.", timestamp=t1 + timedelta(minutes=7, seconds=4)),
        EventIngest(
            role="assistant",
            tool_name="Write",
            tool_input_json={
                "file_path": "apps/zerg/backend/zerg/services/agents_store.py",
                "content": "async def _cosine_search(embedding: list[float], days_back: int, limit: int, project: str | None) -> list[Session]:\n    # Fetch candidate sessions with stored embeddings, compute cosine similarity\n    ...",
            },
            timestamp=t1 + timedelta(minutes=9),
        ),
        EventIngest(role="tool", tool_output_text="File written successfully.", timestamp=t1 + timedelta(minutes=9, seconds=3)),
        # Parallel test + lint
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s1_10",
            tool_input_json={"command": "cd apps/zerg/backend && uv run pytest tests_lite/test_agents_store.py -x -q"},
            timestamp=t1 + timedelta(minutes=12),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s1_11",
            tool_input_json={"command": "cd apps/zerg/backend && uv run ruff check zerg/services/agents_store.py"},
            timestamp=t1 + timedelta(minutes=12),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s1_10",
            tool_output_text=_lh("8.441", 0, 312, "......................................\n38 passed in 8.4s"),
            timestamp=t1 + timedelta(minutes=12, seconds=10),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s1_11",
            tool_output_text=_lh("0.891", 0, 45, "All checks passed."),
            timestamp=t1 + timedelta(minutes=12, seconds=11),
        ),
        EventIngest(
            role="assistant",
            content_text="Tests pass, no lint errors. Now wiring the `?semantic=true` query param to the API endpoint and updating the frontend search mode toggle.",
            timestamp=t1 + timedelta(minutes=13),
        ),
        EventIngest(
            role="assistant",
            tool_name="Edit",
            tool_input_json={
                "file_path": "apps/zerg/backend/zerg/routers/agents.py",
                "old_string": "async def list_sessions(query: str | None = None,",
                "new_string": "async def list_sessions(query: str | None = None, semantic: bool = False,",
            },
            timestamp=t1 + timedelta(minutes=15),
        ),
        EventIngest(role="tool", tool_output_text="File updated successfully.", timestamp=t1 + timedelta(minutes=15, seconds=3)),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_input_json={"command": "cd apps/zerg/backend && uv run pytest tests_lite/ -q --tb=short"},
            timestamp=t1 + timedelta(minutes=17),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh("18.221", 0, 640, ".................................................\n87 passed in 18.2s"),
            timestamp=t1 + timedelta(minutes=17, seconds=20),
        ),
        EventIngest(
            role="assistant",
            content_text="Semantic search is live. The `GET /api/agents/sessions?query=...&semantic=true` endpoint now uses cosine similarity over 256-dim embeddings. Falls back to FTS5 if no embedding key configured. Ready to commit.",
            timestamp=t1 + timedelta(minutes=19),
        ),
    ]

    # -------------------------------------------------------------------------
    # Session 2: Codex — Race condition debug (longhouse)
    # Medium session, lots of parallel reads (Codex style), 20 events
    # -------------------------------------------------------------------------
    t2 = anchor - timedelta(days=3, hours=5)
    s2 = [
        EventIngest(
            role="user",
            content_text="Sessions are occasionally ingested twice — duplicate rows in the DB. Only happens under load. Debug it.",
            timestamp=t2,
        ),
        EventIngest(
            role="assistant",
            content_text="Sounds like a race condition in the upsert path. Let me trace the ingest flow.",
            timestamp=t2 + timedelta(seconds=10),
        ),
        # Parallel file reads (Codex fires these in batches)
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_call_id="call_s2_01",
            tool_input_json={"cmd": "grep -n 'INSERT\\|upsert\\|on_conflict' apps/zerg/backend/zerg/services/agents_store.py | head -30"},
            timestamp=t2 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_call_id="call_s2_02",
            tool_input_json={
                "cmd": "grep -n 'ingest_session\\|provider_session_id' apps/zerg/backend/zerg/services/agents_store.py | head -20"
            },
            timestamp=t2 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_call_id="call_s2_03",
            tool_input_json={"cmd": "grep -rn 'ingest_session' apps/zerg/backend/ --include='*.py' | grep -v test | grep -v __pycache__"},
            timestamp=t2 + timedelta(minutes=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s2_01",
            tool_output_text=_lh(
                "0.088",
                0,
                156,
                "agents_store.py:89:    existing = db.query(Session).filter_by(provider_session_id=s.provider_session_id).first()\nagents_store.py:91:    if existing:\nagents_store.py:94:    session = Session(**fields)\nagents_store.py:95:    db.add(session)",
            ),
            timestamp=t2 + timedelta(minutes=1, seconds=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s2_02",
            tool_output_text=_lh(
                "0.071",
                0,
                98,
                "agents_store.py:89:    existing = db.query(Session).filter_by(provider_session_id=s.provider_session_id).first()\nagents_store.py:102:    def ingest_session(self, s: SessionIngest):",
            ),
            timestamp=t2 + timedelta(minutes=1, seconds=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s2_03",
            tool_output_text=_lh(
                "0.143",
                0,
                112,
                "zerg/services/agents_store.py:102:    def ingest_session(self, s: SessionIngest):\nzerg/api/ingest.py:44:        store.ingest_session(parsed)\nzerg/jobs/session_reflection.py:88:    store.ingest_session(backfill_session)",
            ),
            timestamp=t2 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="assistant",
            content_text="Found it. Line 89: `filter_by(...).first()` then `db.add(session)` — classic check-then-insert race. Two concurrent requests both see no existing row, both insert. Need to use INSERT OR IGNORE / ON CONFLICT at the DB level.",
            timestamp=t2 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_call_id="call_s2_04",
            tool_input_json={"cmd": "sed -n '85,110p' apps/zerg/backend/zerg/services/agents_store.py"},
            timestamp=t2 + timedelta(minutes=3),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_call_id="call_s2_05",
            tool_input_json={"cmd": "grep -n 'provider_session_id' apps/zerg/backend/zerg/models/agents.py"},
            timestamp=t2 + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s2_04",
            tool_output_text=_lh(
                "0.032",
                0,
                201,
                "def ingest_session(self, s: SessionIngest) -> IngestResult:\n    existing = db.query(Session).filter_by(\n        provider_session_id=s.provider_session_id\n    ).first()\n    if existing:\n        return self._update_session(existing, s)\n    session = Session(**self._session_fields(s))\n    db.add(session)\n    db.flush()",
            ),
            timestamp=t2 + timedelta(minutes=3, seconds=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s2_05",
            tool_output_text=_lh("0.041", 0, 88, "agents.py:34:    provider_session_id = Column(String, nullable=True, index=True)"),
            timestamp=t2 + timedelta(minutes=3, seconds=1),
        ),
        EventIngest(
            role="assistant",
            content_text="No UNIQUE constraint on `provider_session_id`. The fix is two-part: add a unique constraint and switch to INSERT OR IGNORE semantics.",
            timestamp=t2 + timedelta(minutes=4),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_input_json={"cmd": "cd apps/zerg/backend && grep -n 'provider_session_id' zerg/models/agents.py"},
            timestamp=t2 + timedelta(minutes=5),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh("0.038", 0, 66, "agents.py:34:    provider_session_id = Column(String, nullable=True, index=True)"),
            timestamp=t2 + timedelta(minutes=5, seconds=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_input_json={"cmd": "cd apps/zerg/backend && uv run pytest tests_lite/test_agents_store.py -x -q -k 'ingest'"},
            timestamp=t2 + timedelta(minutes=12),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh(
                "4.182",
                0,
                189,
                "test_ingest_deduplication PASSED\ntest_ingest_concurrent PASSED\ntest_ingest_update PASSED\n3 passed in 4.2s",
            ),
            timestamp=t2 + timedelta(minutes=12, seconds=5),
        ),
        EventIngest(
            role="assistant",
            content_text="Fixed. Added UNIQUE constraint on `(provider_session_id)` with a `_migrate_agents_columns()` entry, and switched `ingest_session` to use SQLite's `INSERT OR IGNORE` so concurrent writes are safe. Tests confirm deduplication works.",
            timestamp=t2 + timedelta(minutes=13),
        ),
    ]

    # -------------------------------------------------------------------------
    # Session 3: Gemini — Sauron job failing silently (sauron)
    # Medium ops/debug session, 18 events
    # -------------------------------------------------------------------------
    t3 = anchor - timedelta(days=3, hours=1)
    s3 = [
        EventIngest(
            role="user", content_text="The daily-digest job ran but sent an empty email. No errors in logs. Figure out why.", timestamp=t3
        ),
        EventIngest(
            role="assistant",
            content_text="Let me check the job output and trace the email generation path.",
            timestamp=t3 + timedelta(seconds=12),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_input_json={"command": "ssh zerg 'docker logs longhouse-david010 --since 24h | grep -i digest'"},
            timestamp=t3 + timedelta(minutes=1),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh(
                "1.882",
                0,
                234,
                "2026-03-03 08:00:01 INFO  daily-digest started\n2026-03-03 08:00:02 INFO  fetching sessions for last 24h\n2026-03-03 08:00:02 INFO  sessions_found=0\n2026-03-03 08:00:02 INFO  daily-digest completed, email sent",
            ),
            timestamp=t3 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="assistant",
            content_text="`sessions_found=0` — the job ran but found no sessions for the past 24h. That's wrong, there were definitely sessions yesterday. The query must have a timezone or window bug.",
            timestamp=t3 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s3_01",
            tool_input_json={"command": "grep -n 'days_back\\|timedelta\\|utc\\|now()' ~/git/sauron-jobs/jobs/daily_digest.py | head -20"},
            timestamp=t3 + timedelta(minutes=3),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s3_02",
            tool_input_json={
                "command": "ssh zerg 'date && docker exec longhouse-david010 python3 -c \"from datetime import datetime; print(datetime.now())\"'"
            },
            timestamp=t3 + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s3_01",
            tool_output_text=_lh(
                "0.091",
                0,
                178,
                "daily_digest.py:42:    cutoff = datetime.now() - timedelta(days=days_back)\n daily_digest.py:45:    sessions = store.get_sessions(after=cutoff)",
            ),
            timestamp=t3 + timedelta(minutes=3, seconds=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s3_02",
            tool_output_text=_lh("1.441", 0, 88, "Wed Mar  3 08:00:01 UTC 2026\n2026-03-03 08:00:01.332891"),
            timestamp=t3 + timedelta(minutes=3, seconds=1),
        ),
        EventIngest(
            role="assistant",
            content_text="Found it. Line 42: `datetime.now()` — naive local time, no timezone. The DB stores UTC timestamps. On the server (UTC), this happens to work, but session timestamps include a UTC `+00:00` offset and the naive comparison silently returns nothing.",
            timestamp=t3 + timedelta(minutes=4),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_input_json={"command": "sed -n '38,50p' ~/git/sauron-jobs/jobs/daily_digest.py"},
            timestamp=t3 + timedelta(minutes=5),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh(
                "0.044",
                0,
                166,
                "def build_digest(days_back: int = 1) -> str:\n    cutoff = datetime.now() - timedelta(days=days_back)\n    sessions = store.get_sessions(after=cutoff)\n    if not sessions:\n        return ''",
            ),
            timestamp=t3 + timedelta(minutes=5, seconds=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="Edit",
            tool_input_json={
                "file_path": "~/git/sauron-jobs/jobs/daily_digest.py",
                "old_string": "    cutoff = datetime.now() - timedelta(days=days_back)",
                "new_string": "    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)",
            },
            timestamp=t3 + timedelta(minutes=6),
        ),
        EventIngest(role="tool", tool_output_text="File updated successfully.", timestamp=t3 + timedelta(minutes=6, seconds=2)),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_input_json={"command": "cd ~/git/sauron-jobs && uv run pytest tests/ -x -q -k digest"},
            timestamp=t3 + timedelta(minutes=8),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh(
                "3.112",
                0,
                198,
                "test_digest_timezone_aware PASSED\ntest_digest_empty_window PASSED\ntest_digest_format PASSED\n3 passed in 3.1s",
            ),
            timestamp=t3 + timedelta(minutes=8, seconds=4),
        ),
        EventIngest(
            role="assistant",
            content_text="Fixed. `datetime.now()` → `datetime.now(timezone.utc)`. The naive comparison against UTC-aware DB timestamps was silently matching nothing. Deployed and verified next run will find sessions correctly.",
            timestamp=t3 + timedelta(minutes=9),
        ),
    ]

    # -------------------------------------------------------------------------
    # Session 4: Claude — HDR watermark detection feature (hdr project)
    # Long session, different project, 26 events
    # -------------------------------------------------------------------------
    t4 = anchor - timedelta(days=2, hours=6)
    s4 = [
        EventIngest(
            role="user",
            content_text="Add watermark detection to the HDR pipeline. Before processing any image, check if it has a visible watermark and reject it with a clear error.",
            timestamp=t4,
        ),
        EventIngest(
            role="assistant",
            content_text="I'll add a pre-processing watermark detection step. Let me survey the current pipeline entry point and what image analysis tools we have.",
            timestamp=t4 + timedelta(seconds=9),
        ),
        EventIngest(
            role="assistant",
            tool_name="Glob",
            tool_call_id="call_s4_01",
            tool_input_json={"pattern": "apps/hdr/backend/**/*.py"},
            timestamp=t4 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s4_02",
            tool_input_json={"command": "grep -rn 'def process\\|pipeline\\|preprocess' apps/hdr/backend/ --include='*.py' | head -20"},
            timestamp=t4 + timedelta(minutes=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s4_01",
            tool_output_text="apps/hdr/backend/hdr/pipeline.py\napps/hdr/backend/hdr/models.py\napps/hdr/backend/hdr/api/upload.py\napps/hdr/backend/hdr/services/processor.py\napps/hdr/backend/hdr/services/storage.py",
            timestamp=t4 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s4_02",
            tool_output_text=_lh(
                "0.198",
                0,
                145,
                "pipeline.py:28:def process_image(img_path: Path, job_id: str) -> ProcessResult:\npipeline.py:34:    img = _load_and_validate(img_path)\npipeline.py:38:    result = _run_hdr_enhancement(img)\nprocessor.py:15:def preprocess(img_path: Path) -> np.ndarray:",
            ),
            timestamp=t4 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="assistant",
            tool_name="Read",
            tool_input_json={"file_path": "apps/hdr/backend/hdr/pipeline.py"},
            timestamp=t4 + timedelta(minutes=2),
        ),
        EventIngest(
            role="tool",
            tool_output_text='def process_image(img_path: Path, job_id: str) -> ProcessResult:\n    """Main pipeline entry point."""\n    img = _load_and_validate(img_path)  # raises on corrupt/invalid\n    result = _run_hdr_enhancement(img)\n    return ProcessResult(job_id=job_id, output_path=result.path, status=\'done\')',
            timestamp=t4 + timedelta(minutes=2, seconds=7),
        ),
        EventIngest(
            role="assistant",
            content_text="The pipeline entry is `process_image` in `pipeline.py`. I'll add `_check_watermark(img)` between load and enhancement. Will use a lightweight CV approach: check for high-frequency text-like regions in the corners using OpenCV.",
            timestamp=t4 + timedelta(minutes=3),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_input_json={"command": "cd apps/hdr/backend && python3 -c 'import cv2; print(cv2.__version__)'"},
            timestamp=t4 + timedelta(minutes=4),
        ),
        EventIngest(role="tool", tool_output_text=_lh("0.441", 0, 22, "4.9.0"), timestamp=t4 + timedelta(minutes=4, seconds=1)),
        EventIngest(
            role="assistant",
            tool_name="Write",
            tool_input_json={
                "file_path": "apps/hdr/backend/hdr/services/watermark.py",
                "content": '"""Watermark detection for HDR pipeline pre-processing.\n\nUses edge density analysis on corner regions to detect visible watermarks.\nFast (<50ms), no model weights required.\n"""\n\nimport cv2\nimport numpy as np\nfrom pathlib import Path\n\n\nWATERMARK_THRESHOLD = 0.15  # edge density above this → likely watermark\n\n\nclass WatermarkDetected(Exception):\n    """Raised when a watermark is detected in the source image."""\n\n\ndef check_watermark(img: np.ndarray) -> None:\n    """Raise WatermarkDetected if the image likely contains a watermark."""\n    h, w = img.shape[:2]\n    corners = [\n        img[:h//6, :w//6],\n        img[:h//6, w - w//6:],\n        img[h - h//6:, :w//6],\n        img[h - h//6:, w - w//6:],\n    ]\n    for corner in corners:\n        gray = cv2.cvtColor(corner, cv2.COLOR_BGR2GRAY)\n        edges = cv2.Canny(gray, 100, 200)\n        density = edges.sum() / (edges.size * 255)\n        if density > WATERMARK_THRESHOLD:\n            raise WatermarkDetected(\n                f"Watermark detected (edge density {density:.2f}). "\n                "Please submit an unmodified original image."\n            )\n',
            },
            timestamp=t4 + timedelta(minutes=6),
        ),
        EventIngest(role="tool", tool_output_text="File written successfully.", timestamp=t4 + timedelta(minutes=6, seconds=3)),
        EventIngest(
            role="assistant",
            tool_name="Edit",
            tool_input_json={
                "file_path": "apps/hdr/backend/hdr/pipeline.py",
                "old_string": "    img = _load_and_validate(img_path)  # raises on corrupt/invalid\n    result = _run_hdr_enhancement(img)",
                "new_string": "    img = _load_and_validate(img_path)  # raises on corrupt/invalid\n    check_watermark(img)  # raises WatermarkDetected if logo/text found\n    result = _run_hdr_enhancement(img)",
            },
            timestamp=t4 + timedelta(minutes=8),
        ),
        EventIngest(role="tool", tool_output_text="File updated successfully.", timestamp=t4 + timedelta(minutes=8, seconds=2)),
        # Parallel: write tests + check existing test structure
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s4_10",
            tool_input_json={"command": "ls apps/hdr/backend/tests/"},
            timestamp=t4 + timedelta(minutes=9),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s4_11",
            tool_input_json={"command": "grep -n 'WatermarkDetected\\|check_watermark' apps/hdr/backend/tests/ -r"},
            timestamp=t4 + timedelta(minutes=9),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s4_10",
            tool_output_text=_lh("0.031", 0, 88, "test_pipeline.py  test_storage.py  test_upload.py  conftest.py"),
            timestamp=t4 + timedelta(minutes=9, seconds=1),
        ),
        EventIngest(
            role="tool", tool_call_id="call_s4_11", tool_output_text=_lh("0.044", 1, 22, ""), timestamp=t4 + timedelta(minutes=9, seconds=1)
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_input_json={"command": "cd apps/hdr/backend && uv run pytest tests/test_pipeline.py -x -q"},
            timestamp=t4 + timedelta(minutes=14),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh(
                "6.881",
                0,
                288,
                "test_pipeline_clean_image PASSED\ntest_pipeline_watermarked_image PASSED\ntest_pipeline_corrupt_image PASSED\ntest_watermark_corners PASSED\n4 passed in 6.9s",
            ),
            timestamp=t4 + timedelta(minutes=14, seconds=8),
        ),
        EventIngest(
            role="assistant",
            content_text="Watermark detection shipped. `check_watermark()` runs in <50ms per image, catches logos/text in corners with >85% accuracy on our test set. Clean images pass through unchanged. `WatermarkDetected` surfaces as a 422 with a user-friendly message.",
            timestamp=t4 + timedelta(minutes=15),
        ),
    ]

    # -------------------------------------------------------------------------
    # Session 5: Codex — Frontend search UI wiring (longhouse)
    # Medium session with parallel reads, 17 events
    # -------------------------------------------------------------------------
    t5 = anchor - timedelta(days=2, hours=1)
    s5 = [
        EventIngest(
            role="user",
            content_text="Wire the semantic search toggle into the sessions timeline UI. Should be a small pill next to the search box.",
            timestamp=t5,
        ),
        EventIngest(
            role="assistant", content_text="I'll find the search component and add a mode toggle.", timestamp=t5 + timedelta(seconds=8)
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_call_id="call_s5_01",
            tool_input_json={"cmd": "find apps/zerg/frontend-web/src -name '*.tsx' | xargs grep -l 'search\\|query' | head -10"},
            timestamp=t5 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_call_id="call_s5_02",
            tool_input_json={
                "cmd": "grep -rn 'useSearch\\|searchQuery\\|query=' apps/zerg/frontend-web/src/pages/SessionsPage.tsx | head -20"
            },
            timestamp=t5 + timedelta(minutes=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s5_01",
            tool_output_text=_lh(
                "0.312",
                0,
                144,
                "apps/zerg/frontend-web/src/pages/SessionsPage.tsx\napps/zerg/frontend-web/src/hooks/useAgentSessions.ts\napps/zerg/frontend-web/src/services/api/agents.ts",
            ),
            timestamp=t5 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s5_02",
            tool_output_text=_lh(
                "0.088",
                0,
                199,
                "SessionsPage.tsx:44:  const [searchQuery, setSearchQuery] = useState('');\nSessionsPage.tsx:58:  const { data } = useAgentSessions({ query: searchQuery });",
            ),
            timestamp=t5 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_call_id="call_s5_03",
            tool_input_json={"cmd": "sed -n '40,80p' apps/zerg/frontend-web/src/pages/SessionsPage.tsx"},
            timestamp=t5 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_call_id="call_s5_04",
            tool_input_json={"cmd": "grep -n 'semantic\\|mode' apps/zerg/frontend-web/src/services/api/agents.ts | head -10"},
            timestamp=t5 + timedelta(minutes=2),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s5_03",
            tool_output_text=_lh(
                "0.029",
                0,
                312,
                "  const [searchQuery, setSearchQuery] = useState('');\n  const { data, isLoading } = useAgentSessions({\n    query: debouncedSearch,\n    days_back: daysFilter,\n  });\n  return (\n    <div className='sessions-search'>\n      <input value={searchQuery} onChange={...} />\n    </div>\n  );",
            ),
            timestamp=t5 + timedelta(minutes=2, seconds=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s5_04",
            tool_output_text=_lh("0.041", 0, 55, "agents.ts:88:  semantic?: boolean;"),
            timestamp=t5 + timedelta(minutes=2, seconds=1),
        ),
        EventIngest(
            role="assistant",
            content_text="The `semantic` param is already in the API type. I just need to add state + a toggle pill in the JSX.",
            timestamp=t5 + timedelta(minutes=3),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_input_json={"cmd": "cd apps/zerg/frontend-web && bun run typecheck 2>&1 | tail -5"},
            timestamp=t5 + timedelta(minutes=10),
        ),
        EventIngest(role="tool", tool_output_text=_lh("8.441", 0, 44, "Found 0 errors."), timestamp=t5 + timedelta(minutes=10, seconds=10)),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_input_json={"cmd": "cd apps/zerg/frontend-web && bun run lint 2>&1 | tail -5"},
            timestamp=t5 + timedelta(minutes=11),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh("4.112", 0, 33, "All files linted. 0 warnings, 0 errors."),
            timestamp=t5 + timedelta(minutes=11, seconds=5),
        ),
        EventIngest(
            role="assistant",
            content_text="UI wired. Added `useSemanticSearch` state and an 'AI' pill toggle next to the search input. When active, appends `semantic=true` to the query. Type check and lint clean.",
            timestamp=t5 + timedelta(minutes=12),
        ),
    ]

    # -------------------------------------------------------------------------
    # Session 6: Claude — Production incident: OOM on zerg server (ops)
    # Urgent debug session, 16 events
    # -------------------------------------------------------------------------
    t6 = anchor - timedelta(days=1, hours=8)
    s6 = [
        EventIngest(
            role="user",
            content_text="Zerg server is hitting 95% memory. Longhouse instance is the culprit. Figure out what's eating RAM and fix it without downtime.",
            timestamp=t6,
        ),
        EventIngest(
            role="assistant", content_text="On it. Checking memory usage and process breakdown.", timestamp=t6 + timedelta(seconds=5)
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s6_01",
            tool_input_json={
                "command": "ssh zerg 'free -h && docker stats --no-stream --format \"{{.Name}}\\t{{.MemUsage}}\" | sort -t/ -k1 -h'"
            },
            timestamp=t6 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s6_02",
            tool_input_json={
                "command": "ssh zerg 'docker exec longhouse-david010 python3 -c \"import tracemalloc; tracemalloc.start(); import zerg.main\"'"
            },
            timestamp=t6 + timedelta(minutes=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s6_01",
            tool_output_text=_lh(
                "1.882",
                0,
                334,
                "              total        used        free\nMem:            15G        14G       512M\nlonghouse-david010\t1.8GiB / 15GiB\nnginx-proxy\t\t48MiB / 15GiB\nlonghouse-control\t312MiB / 15GiB",
            ),
            timestamp=t6 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s6_02",
            tool_output_text=_lh("3.441", 1, 88, "Error: cannot attach to running process"),
            timestamp=t6 + timedelta(minutes=1, seconds=3),
        ),
        EventIngest(
            role="assistant",
            content_text="Longhouse instance at 1.8GB — way above baseline (~350MB). Let me check what's grown: likely embedding backfill job or an unbounded in-memory cache.",
            timestamp=t6 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_input_json={
                "command": "ssh zerg 'docker logs longhouse-david010 --since 2h | grep -E \"embedding|backfill|cache|OOM|ERROR\" | tail -30'"
            },
            timestamp=t6 + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh(
                "1.773",
                0,
                412,
                "2026-03-03 06:12:01 INFO  Embedding backfill started: 12,441 sessions\n2026-03-03 06:12:01 INFO  Embedding backfill: batch 1/249\n2026-03-03 06:14:22 INFO  Embedding backfill: batch 38/249\n2026-03-03 08:00:01 INFO  Embedding backfill: batch 102/249\n(process still running after 2h)",
            ),
            timestamp=t6 + timedelta(minutes=3, seconds=2),
        ),
        EventIngest(
            role="assistant",
            content_text="Backfill job is loading all 12k sessions into memory before batching. It's accumulating embedding vectors (each ~1KB) without releasing. Need to stop the job, add streaming/chunked processing, and restart.",
            timestamp=t6 + timedelta(minutes=4),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_input_json={"command": "ssh zerg 'docker exec longhouse-david010 kill -SIGTERM $(pgrep -f backfill_embeddings)'"},
            timestamp=t6 + timedelta(minutes=5),
        ),
        EventIngest(role="tool", tool_output_text=_lh("1.112", 0, 22, ""), timestamp=t6 + timedelta(minutes=5, seconds=2)),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_input_json={"command": "ssh zerg 'sleep 10 && free -h | grep Mem'"},
            timestamp=t6 + timedelta(minutes=6),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh("11.221", 0, 55, "Mem:            15G       5.1G        9.9G"),
            timestamp=t6 + timedelta(minutes=6, seconds=12),
        ),
        EventIngest(
            role="assistant",
            content_text="Memory back to 5.1GB (healthy). Backfill job killed. Root cause: `backfill_embeddings` loaded all sessions into a list before yielding — converted to a generator with `yield from` and added explicit `del` after each batch. Backfill will resume automatically on next scheduled run with the fixed code.",
            timestamp=t6 + timedelta(minutes=8),
        ),
    ]

    # -------------------------------------------------------------------------
    # Session 7: Gemini — Add Recall panel to sessions page (longhouse)
    # Medium feature session, 15 events
    # -------------------------------------------------------------------------
    t7 = anchor - timedelta(days=1, hours=3)
    s7 = [
        EventIngest(
            role="user",
            content_text="Add a 'Recall' panel to the sessions timeline. It should show semantically related past turns when you click a session.",
            timestamp=t7,
        ),
        EventIngest(
            role="assistant",
            content_text="I'll add a side panel that calls the `/api/agents/recall` endpoint when a session is selected.",
            timestamp=t7 + timedelta(seconds=10),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s7_01",
            tool_input_json={"command": "grep -rn 'recall\\|RecallPanel' apps/zerg/frontend-web/src/ --include='*.tsx' | head -10"},
            timestamp=t7 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s7_02",
            tool_input_json={
                "command": "curl -s 'http://localhost:47300/api/agents/recall?query=test&limit=3' | python3 -m json.tool | head -30"
            },
            timestamp=t7 + timedelta(minutes=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s7_01",
            tool_output_text=_lh("0.188", 0, 88, "apps/zerg/frontend-web/src/components/RecallPanel.tsx"),
            timestamp=t7 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s7_02",
            tool_output_text=_lh(
                "0.441",
                0,
                312,
                '{\n  "turns": [\n    {"session_id": "abc...", "role": "user", "content": "Fix the migration", "score": 0.91},\n    {"session_id": "def...", "role": "assistant", "content": "Adding the column...", "score": 0.87}\n  ]\n}',
            ),
            timestamp=t7 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="assistant",
            content_text="RecallPanel component exists but isn't connected to session selection. I'll wire it via a context/state update when a session card is focused.",
            timestamp=t7 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            tool_name="Read",
            tool_input_json={"file_path": "apps/zerg/frontend-web/src/components/RecallPanel.tsx"},
            timestamp=t7 + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_output_text="export function RecallPanel({ query }: { query: string }) {\n  const { data } = useRecall(query);\n  // renders turn snippets\n}",
            timestamp=t7 + timedelta(minutes=3, seconds=5),
        ),
        EventIngest(
            role="assistant",
            tool_name="Edit",
            tool_input_json={
                "file_path": "apps/zerg/frontend-web/src/pages/SessionsPage.tsx",
                "old_string": "  const [selectedSession, setSelectedSession] = useState(null);",
                "new_string": "  const [selectedSession, setSelectedSession] = useState(null);\n  const recallQuery = selectedSession?.first_user_message ?? '';",
            },
            timestamp=t7 + timedelta(minutes=5),
        ),
        EventIngest(role="tool", tool_output_text="File updated successfully.", timestamp=t7 + timedelta(minutes=5, seconds=2)),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_input_json={"command": "cd apps/zerg/frontend-web && bun run typecheck 2>&1 | tail -3"},
            timestamp=t7 + timedelta(minutes=9),
        ),
        EventIngest(role="tool", tool_output_text=_lh("7.881", 0, 33, "Found 0 errors."), timestamp=t7 + timedelta(minutes=9, seconds=9)),
        EventIngest(
            role="assistant",
            content_text="Recall panel is wired. Selecting a session now populates the RecallPanel with semantically similar past turns. No new API endpoints needed — `useRecall` hook already existed.",
            timestamp=t7 + timedelta(minutes=10),
        ),
    ]

    # -------------------------------------------------------------------------
    # Session 8: Claude — Multi-repo research (various projects)
    # Short research/MCP-heavy session, 10 events
    # -------------------------------------------------------------------------
    t8 = anchor - timedelta(hours=5)
    s8 = [
        EventIngest(
            role="user",
            content_text="What are the most common failure patterns across my projects this week? Give me a cross-project summary.",
            timestamp=t8,
        ),
        EventIngest(
            role="assistant", content_text="I'll query insights and session data across all projects.", timestamp=t8 + timedelta(seconds=8)
        ),
        EventIngest(
            role="assistant",
            tool_name="mcp__longhouse__query_insights",
            tool_call_id="call_s8_01",
            tool_input_json={"since_hours": 168, "insight_type": "failure", "limit": 20},
            timestamp=t8 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="mcp__longhouse__search_sessions",
            tool_call_id="call_s8_02",
            tool_input_json={"query": "error failed broken", "days_back": 7, "limit": 10},
            timestamp=t8 + timedelta(minutes=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s8_01",
            tool_output_text='{"insights": [{"project": "longhouse", "title": "Race condition in ingest upsert", "severity": "warning"}, {"project": "sauron", "title": "Naive datetime in digest job", "severity": "warning"}, {"project": "longhouse", "title": "Embedding backfill OOM under 10k sessions", "severity": "critical"}]}',
            timestamp=t8 + timedelta(minutes=1, seconds=3),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s8_02",
            tool_output_text='{"sessions": [{"project": "longhouse", "summary_title": "Production OOM incident"}, {"project": "sauron", "summary_title": "Daily digest sending empty emails"}, {"project": "hdr", "summary_title": "Upload pipeline rejecting valid images"}]}',
            timestamp=t8 + timedelta(minutes=1, seconds=3),
        ),
        EventIngest(
            role="assistant",
            tool_name="mcp__search-hub__web_research",
            tool_input_json={
                "task": "best practices for preventing Python memory leaks in long-running async services",
                "source": "openai",
            },
            timestamp=t8 + timedelta(minutes=2),
        ),
        EventIngest(
            role="tool",
            tool_output_text="Key patterns: (1) Generator pipelines over list accumulation for bulk processing. (2) Explicit `del` + `gc.collect()` after batch operations. (3) Use `tracemalloc` snapshots in staging. (4) WeakRef caches for large object pools. (5) Profile with `memray` for production-safe heap snapshots.",
            timestamp=t8 + timedelta(minutes=3, seconds=30),
        ),
        EventIngest(
            role="assistant",
            content_text="**Cross-project failure summary (past 7 days):**\n\n- **longhouse** (2 issues): Race condition in session ingest upsert (fixed), OOM in embedding backfill job — both resolved this week.\n- **sauron** (1 issue): Naive `datetime.now()` vs UTC-aware DB timestamps in digest job — fixed.\n- **hdr** (1 issue): Watermark false-positives rejecting valid images — threshold tuned, tests added.\n\nCommon thread: **timezone/UTC discipline** and **generator vs list** patterns appear in 3/4 incidents. Worth adding linting rules for both.",
            timestamp=t8 + timedelta(minutes=5),
        ),
    ]

    # -------------------------------------------------------------------------
    # Session 9: Codex — Longhouse engine build fix (longhouse)
    # Short, quick fix session, 9 events
    # -------------------------------------------------------------------------
    t9 = anchor - timedelta(hours=2)
    s9 = [
        EventIngest(
            role="user",
            content_text="The Rust engine build is failing on the self-hosted CI runner. Works locally. Check it.",
            timestamp=t9,
        ),
        EventIngest(
            role="assistant",
            content_text="Checking CI logs and local vs runner environment differences.",
            timestamp=t9 + timedelta(seconds=9),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_input_json={"cmd": "gh run list --workflow=runtime-image.yml --limit 3 --json status,conclusion,databaseId,headBranch"},
            timestamp=t9 + timedelta(minutes=1),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh(
                "1.441",
                0,
                188,
                '[{"status":"completed","conclusion":"failure","databaseId":12841,"headBranch":"main"},{"status":"completed","conclusion":"success","databaseId":12830,"headBranch":"main"}]',
            ),
            timestamp=t9 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_input_json={"cmd": "gh run view 12841 --log-failed 2>&1 | grep -A5 'error\\[' | head -30"},
            timestamp=t9 + timedelta(minutes=2),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh(
                "2.881",
                0,
                312,
                "error[E0432]: unresolved import `ring::rand`\n --> apps/engine/src/crypto.rs:3:5\nerror: aborting due to previous error\nnote: the `ring` crate requires OpenSSL headers; install `libssl-dev` on the runner",
            ),
            timestamp=t9 + timedelta(minutes=2, seconds=3),
        ),
        EventIngest(
            role="assistant",
            content_text="Missing `libssl-dev` on the CI runner. Need to add it to the workflow `apt-get install` step.",
            timestamp=t9 + timedelta(minutes=3),
        ),
        EventIngest(
            role="assistant",
            tool_name="exec_command",
            tool_input_json={"cmd": "grep -n 'apt-get\\|libssl\\|openssl' .github/workflows/runtime-image.yml"},
            timestamp=t9 + timedelta(minutes=4),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh("0.044", 0, 88, "runtime-image.yml:31:        run: sudo apt-get install -y pkg-config"),
            timestamp=t9 + timedelta(minutes=4, seconds=1),
        ),
        EventIngest(
            role="assistant",
            content_text="Added `libssl-dev` to the apt-get line. Triggered re-run — build passed.",
            timestamp=t9 + timedelta(minutes=8),
        ),
    ]

    # -------------------------------------------------------------------------
    # Session 10: Claude — Current session (in progress, no ended_at)
    # Today, active work, 12 events
    # -------------------------------------------------------------------------
    t10 = anchor - timedelta(hours=1)
    s10 = [
        EventIngest(role="user", content_text="we need to update landing page images, i have made some changes to the UI.", timestamp=t10),
        EventIngest(
            role="assistant",
            content_text="Let me check the current screenshot setup and what's changed in the UI.",
            timestamp=t10 + timedelta(seconds=9),
        ),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_call_id="call_s10_01",
            tool_input_json={"command": "ls -lh apps/zerg/frontend-web/public/images/landing/"},
            timestamp=t10 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="Read",
            tool_call_id="call_s10_02",
            tool_input_json={"file_path": "scripts/screenshots.yaml"},
            timestamp=t10 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            tool_name="Read",
            tool_call_id="call_s10_03",
            tool_input_json={"file_path": "scripts/capture_marketing.py"},
            timestamp=t10 + timedelta(minutes=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s10_01",
            tool_output_text=_lh(
                "0.031",
                0,
                112,
                "-rw-r--r--  1 user  staff  803K Feb 27 timeline-preview.png\n-rw-r--r--  1 user  staff  683K Feb 27 session-detail-preview.png\n-rw-r--r--  1 user  staff  787K Feb 27 search-preview.png",
            ),
            timestamp=t10 + timedelta(minutes=1, seconds=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s10_02",
            tool_output_text="base_url: http://localhost:30080\nscreenshots:\n  timeline-preview:\n    url: /timeline?marketing=true\n    viewport: { width: 1400, height: 900 }",
            timestamp=t10 + timedelta(minutes=1, seconds=1),
        ),
        EventIngest(
            role="tool",
            tool_call_id="call_s10_03",
            tool_output_text="# Marketing screenshot capture script\n# Requires dev stack running on base_url port",
            timestamp=t10 + timedelta(minutes=1, seconds=2),
        ),
        EventIngest(
            role="assistant",
            content_text="Images are from Feb 27, need refreshing. The capture script expects Docker stack on :30080 but you're running SQLite dev on :47200. I'll create a self-contained script and a `make marketing-screenshots` target.",
            timestamp=t10 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            tool_name="Write",
            tool_input_json={
                "file_path": "scripts/marketing-screenshots.sh",
                "content": "#!/usr/bin/env bash\n# Self-contained: starts demo stack, captures, exits\n...",
            },
            timestamp=t10 + timedelta(minutes=5),
        ),
        EventIngest(role="tool", tool_output_text="File written successfully.", timestamp=t10 + timedelta(minutes=5, seconds=2)),
        EventIngest(
            role="assistant",
            tool_name="Bash",
            tool_input_json={"command": "make marketing-screenshots"},
            timestamp=t10 + timedelta(minutes=6),
        ),
        EventIngest(
            role="tool",
            tool_output_text=_lh(
                "47.221",
                0,
                445,
                "Building demo database...\nBackend ready\nFrontend ready\n\nCapturing 3 screenshots...\n  timeline-preview (820 KB)\n  search-preview (817 KB)\n  session-detail-preview (706 KB)\n\nDone! Captured 3 screenshots.",
            ),
            timestamp=t10 + timedelta(minutes=6, seconds=50),
        ),
    ]

    return [
        SessionIngest(
            provider="claude",
            environment="production",
            project="longhouse",
            device_id="shipper-laptop",
            cwd="/Users/demo/git/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="feat/semantic-search",
            started_at=t1,
            ended_at=t1 + timedelta(minutes=22),
            provider_session_id="demo-claude-01",
            events=s1,
        ),
        SessionIngest(
            provider="codex",
            environment="production",
            project="longhouse",
            device_id="shipper-laptop",
            cwd="/Users/demo/git/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="fix/ingest-race-condition",
            started_at=t2,
            ended_at=t2 + timedelta(minutes=16),
            provider_session_id="demo-codex-01",
            events=s2,
        ),
        SessionIngest(
            provider="gemini",
            environment="production",
            project="sauron",
            device_id="shipper-laptop",
            cwd="/Users/demo/git/sauron-jobs",
            git_repo="https://github.com/demo/sauron-jobs",
            git_branch="fix/digest-timezone",
            started_at=t3,
            ended_at=t3 + timedelta(minutes=12),
            provider_session_id="demo-gemini-01",
            events=s3,
        ),
        SessionIngest(
            provider="claude",
            environment="production",
            project="hdr",
            device_id="shipper-laptop",
            cwd="/Users/demo/git/hdr",
            git_repo="https://github.com/demo/hdr",
            git_branch="feat/watermark-detection",
            started_at=t4,
            ended_at=t4 + timedelta(minutes=18),
            provider_session_id="demo-claude-02",
            events=s4,
        ),
        SessionIngest(
            provider="codex",
            environment="production",
            project="longhouse",
            device_id="shipper-laptop",
            cwd="/Users/demo/git/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="feat/semantic-search",
            started_at=t5,
            ended_at=t5 + timedelta(minutes=14),
            provider_session_id="demo-codex-02",
            events=s5,
        ),
        SessionIngest(
            provider="claude",
            environment="production",
            project="longhouse",
            device_id="shipper-laptop",
            cwd="/Users/demo/git/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="main",
            started_at=t6,
            ended_at=t6 + timedelta(minutes=10),
            provider_session_id="demo-claude-03",
            events=s6,
        ),
        SessionIngest(
            provider="gemini",
            environment="production",
            project="longhouse",
            device_id="shipper-laptop",
            cwd="/Users/demo/git/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="feat/recall-panel",
            started_at=t7,
            ended_at=t7 + timedelta(minutes=12),
            provider_session_id="demo-gemini-02",
            events=s7,
        ),
        SessionIngest(
            provider="claude",
            environment="production",
            project=None,
            device_id="shipper-laptop",
            cwd="/Users/demo/git",
            git_repo=None,
            git_branch=None,
            started_at=t8,
            ended_at=t8 + timedelta(minutes=7),
            provider_session_id="demo-claude-04",
            events=s8,
        ),
        SessionIngest(
            provider="codex",
            environment="production",
            project="longhouse",
            device_id="shipper-laptop",
            cwd="/Users/demo/git/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="main",
            started_at=t9,
            ended_at=t9 + timedelta(minutes=9),
            provider_session_id="demo-codex-03",
            events=s9,
        ),
        SessionIngest(
            provider="claude",
            environment="production",
            project="longhouse",
            device_id="shipper-laptop",
            cwd="/Users/demo/git/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="main",
            started_at=t10,
            ended_at=None,  # in-progress session
            provider_session_id="demo-claude-05",
            events=s10,
        ),
    ]
