#!/usr/bin/env python3
"""Fast embedding backfill using batched OpenAI API calls.

Sends up to 100 texts per API call instead of 1-by-1.
~10-20x faster than the sequential version.

Run directly on the server:
    docker exec longhouse-david python3 /data/backfill_embeddings.py

Or locally against a DB:
    DATABASE_URL=sqlite:///path/to/db.sqlite python3 scripts/backfill_embeddings.py
"""

import asyncio
import os
import sys
import time

# Add backend to path — works both in container (/app/) and locally (relative to script)
_script_dir = os.path.dirname(os.path.abspath(__file__))
_backend_rel = os.path.join(_script_dir, "..", "apps", "zerg", "backend")
_backend_container = "/app/apps/zerg/backend"
if os.path.isdir(_backend_container):
    sys.path.insert(0, _backend_container)
elif os.path.isdir(_backend_rel):
    sys.path.insert(0, os.path.abspath(_backend_rel))

# Batch size: how many texts to send in one API call
# OpenAI supports up to 2048 inputs per call, but keep reasonable for memory
BATCH_TEXTS = 100
# How many sessions to prepare per DB fetch
BATCH_SESSIONS = 50


async def batch_embed(texts: list[str], config) -> list:
    """Embed multiple texts in a single API call."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=config.api_key)
    try:
        response = await client.embeddings.create(
            model=config.model,
            input=texts,
            dimensions=config.dims,
        )
        return [d.embedding for d in response.data]
    finally:
        await client.close()


async def main():
    import numpy as np
    from sqlalchemy import text as sa_text

    from zerg.database import make_engine, make_sessionmaker
    from zerg.models.agents import AgentEvent, AgentSession, SessionEmbedding
    from zerg.models_config import get_embedding_config
    from zerg.services.session_processing.embeddings import (
        embedding_to_bytes,
        prepare_session_chunk,
        prepare_turn_chunks,
    )

    db_url = os.environ.get("DATABASE_URL", "sqlite:////data/longhouse.db")
    config = get_embedding_config()
    if not config:
        print("ERROR: No embedding config — set OPENAI_API_KEY")
        sys.exit(1)

    print(f"Embedding provider: {config.provider}, model: {config.model}, dims: {config.dims}")
    print(f"Batch size: {BATCH_TEXTS} texts/call, {BATCH_SESSIONS} sessions/fetch")

    engine = make_engine(db_url)
    Session = make_sessionmaker(engine)

    with Session() as db:
        total = db.execute(sa_text("SELECT COUNT(*) FROM sessions WHERE needs_embedding = 1")).scalar()
    print(f"Sessions needing embeddings: {total}")

    if total == 0:
        print("Nothing to do.")
        return

    embedded = 0
    skipped = 0
    errors = 0
    start = time.time()

    while True:
        # Fetch batch of session IDs
        with Session() as db:
            rows = db.execute(
                sa_text(f"SELECT id FROM sessions WHERE needs_embedding = 1 LIMIT {BATCH_SESSIONS}")
            ).fetchall()

        if not rows:
            break

        # Phase 1: Prepare all chunks for this batch (CPU only, no API calls)
        # Each entry: (session_id, chunk, text_index_in_batch)
        all_texts = []
        chunk_map = []  # (session_id, chunk) per text

        session_ids_processed = []

        for (sid,) in rows:
            sid = str(sid)
            try:
                with Session() as db:
                    session = db.query(AgentSession).filter(AgentSession.id == sid).first()
                    if not session:
                        skipped += 1
                        continue

                    events = (
                        db.query(AgentEvent)
                        .filter(AgentEvent.session_id == sid)
                        .order_by(AgentEvent.timestamp)
                        .all()
                    )

                    if not events:
                        db.execute(
                            sa_text("UPDATE sessions SET needs_embedding = 0 WHERE id = :sid"),
                            {"sid": sid},
                        )
                        db.commit()
                        skipped += 1
                        continue

                    # Convert events to dicts
                    event_dicts = [
                        {
                            "role": e.role,
                            "content_text": e.content_text,
                            "tool_name": e.tool_name,
                            "tool_input_json": e.tool_input_json,
                            "tool_output_text": e.tool_output_text,
                            "timestamp": e.timestamp,
                            "session_id": str(e.session_id),
                        }
                        for e in events
                    ]

                    # Prepare chunks
                    session_chunk = prepare_session_chunk(session, event_dicts)
                    if session_chunk:
                        all_texts.append(session_chunk.text)
                        chunk_map.append((sid, session_chunk))

                    turn_chunks = prepare_turn_chunks(event_dicts)
                    for chunk in turn_chunks:
                        all_texts.append(chunk.text)
                        chunk_map.append((sid, chunk))

                    session_ids_processed.append(sid)

            except Exception as exc:
                print(f"  PREP ERROR {sid}: {type(exc).__name__}: {exc}")
                errors += 1

        if not all_texts:
            continue

        # Phase 2: Batch API calls
        all_vectors = []
        api_errors = 0
        for i in range(0, len(all_texts), BATCH_TEXTS):
            batch = all_texts[i : i + BATCH_TEXTS]
            try:
                vecs = await batch_embed(batch, config)
                all_vectors.extend(vecs)
            except Exception as exc:
                print(f"  API ERROR batch {i}-{i+len(batch)}: {type(exc).__name__}: {exc}")
                # Fill with None for failed batch
                all_vectors.extend([None] * len(batch))
                api_errors += len(batch)

        # Phase 3: Write to DB in one transaction
        with Session() as db:
            for idx, (sid, chunk) in enumerate(chunk_map):
                vec = all_vectors[idx] if idx < len(all_vectors) else None
                if vec is None:
                    continue

                vec_bytes = embedding_to_bytes(np.array(vec, dtype=np.float32))

                existing = (
                    db.query(SessionEmbedding)
                    .filter(
                        SessionEmbedding.session_id == sid,
                        SessionEmbedding.kind == chunk.kind,
                        SessionEmbedding.chunk_index == chunk.chunk_index,
                        SessionEmbedding.model == config.model,
                    )
                    .first()
                )
                if existing:
                    existing.embedding = vec_bytes
                    existing.content_hash = chunk.content_hash
                    existing.dims = config.dims
                    if chunk.kind == "turn":
                        existing.event_index_start = chunk.event_index_start
                        existing.event_index_end = chunk.event_index_end
                else:
                    row = SessionEmbedding(
                        session_id=sid,
                        kind=chunk.kind,
                        chunk_index=chunk.chunk_index,
                        model=config.model,
                        dims=config.dims,
                        embedding=vec_bytes,
                        content_hash=chunk.content_hash,
                    )
                    if chunk.kind == "turn":
                        row.event_index_start = chunk.event_index_start
                        row.event_index_end = chunk.event_index_end
                    db.add(row)

            # Clear needs_embedding for all processed sessions
            for sid in session_ids_processed:
                db.execute(
                    sa_text("UPDATE sessions SET needs_embedding = 0 WHERE id = :sid"),
                    {"sid": sid},
                )
            db.commit()

        embedded += len(session_ids_processed)
        errors += api_errors

        elapsed = time.time() - start
        rate = embedded / elapsed if elapsed > 0 else 0
        remaining = total - embedded - skipped - errors
        eta = remaining / rate if rate > 0 else 0
        print(
            f"  [{embedded + skipped}/{total}] "
            f"embedded={embedded} skipped={skipped} errors={errors} "
            f"texts={len(all_texts)} "
            f"({rate:.1f} sessions/s, ~{eta/60:.0f}m remaining)"
        )

        # WAL checkpoint periodically
        if embedded % 200 == 0:
            try:
                with engine.connect() as conn:
                    conn.execute(sa_text("PRAGMA wal_checkpoint(TRUNCATE)"))
            except Exception:
                pass

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.0f}s: {embedded} embedded, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    asyncio.run(main())
