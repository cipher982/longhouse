#!/usr/bin/env python3
"""One-shot embedding backfill script.

Run directly on the server:
    docker exec longhouse-david python3 /app/scripts/backfill_embeddings.py

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


async def main():
    from sqlalchemy import text as sa_text

    from zerg.database import make_engine, make_sessionmaker
    from zerg.models.agents import AgentEvent, AgentSession
    from zerg.models_config import get_embedding_config
    from zerg.services.session_processing.embeddings import embed_session

    db_url = os.environ.get("DATABASE_URL", "sqlite:////data/longhouse.db")
    config = get_embedding_config()
    if not config:
        print("ERROR: No embedding config — set OPENAI_API_KEY")
        sys.exit(1)

    print(f"Embedding provider: {config.provider}, model: {config.model}, dims: {config.dims}")

    engine = make_engine(db_url)
    Session = make_sessionmaker(engine)

    # Count
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
        # Fetch next batch of IDs
        with Session() as db:
            rows = db.execute(
                sa_text("SELECT id FROM sessions WHERE needs_embedding = 1 LIMIT 20")
            ).fetchall()

        if not rows:
            break

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

                    count = await embed_session(sid, session, events, config, db)
                    embedded += 1

                    elapsed = time.time() - start
                    rate = embedded / elapsed if elapsed > 0 else 0
                    remaining = total - embedded - skipped - errors
                    eta = remaining / rate if rate > 0 else 0
                    print(
                        f"  [{embedded + skipped + errors}/{total}] "
                        f"embedded={embedded} skipped={skipped} errors={errors} "
                        f"({rate:.1f}/s, ~{eta/60:.0f}m remaining)"
                    )

            except Exception as exc:
                print(f"  ERROR {sid}: {type(exc).__name__}: {exc}")
                errors += 1

        # WAL checkpoint every 100
        if (embedded + skipped + errors) % 100 == 0:
            try:
                with engine.connect() as conn:
                    conn.execute(sa_text("PRAGMA wal_checkpoint(TRUNCATE)"))
            except Exception:
                pass

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.0f}s: {embedded} embedded, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    asyncio.run(main())
