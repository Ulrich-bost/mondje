import asyncio
import logging
import os

from . import db
from .parsing import read_batch

log = logging.getLogger("mondje.import")

BATCH = int(os.getenv("IMPORT_BATCH", "5000"))
LOCK_KEY = 42
slots = asyncio.Semaphore(int(os.getenv("IMPORT_WORKERS", "10")))
tasks = {}
cancels = {}

INSERT_SQL = """
INSERT INTO transactions (id, type, amount, description, agency, import_id, source_line)
SELECT t.id, t.type, t.amount, t.description, $6::text, $7::uuid, t.line
FROM unnest($1::uuid[], $2::text[], $3::bigint[], $4::text[], $5::bigint[]) AS t(id, type, amount, description, line)
ORDER BY t.line
ON CONFLICT (id) DO NOTHING
RETURNING id
"""

ERROR_SQL = """
INSERT INTO import_errors (import_id, line_no, byte_start, byte_end, col, content, reason)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

PROGRESS_SQL = """
UPDATE imports SET
  bytes_offset = $2,
  lines_read = $3,
  inserted = inserted + $4,
  duplicates = duplicates + $5,
  errors = errors + $6,
  status = CASE WHEN $7::boolean THEN 'done' ELSE status END,
  finished_at = CASE WHEN $7::boolean THEN now() ELSE finished_at END
WHERE id = $1 AND status = 'running'
"""


def launch(import_id):
    cancels[import_id] = asyncio.Event()
    tasks[import_id] = asyncio.create_task(run(import_id))


async def resume_all():
    async with db.pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM imports WHERE status = 'running'")
    for row in rows:
        log.info("reprise de l'import %s", row["id"])
        launch(row["id"])


async def run(import_id):
    try:
        async with slots:
            await work(import_id)
    except Exception as exc:
        log.exception("échec de l'import %s", import_id)
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE imports SET status = 'failed', failure = $2, finished_at = now() WHERE id = $1 AND status = 'running'",
                import_id,
                str(exc)[:500],
            )
    finally:
        tasks.pop(import_id, None)
        cancels.pop(import_id, None)


async def work(import_id):
    cancel = cancels[import_id]
    async with db.pool.acquire() as conn:
        imp = await conn.fetchrow("SELECT * FROM imports WHERE id = $1", import_id)
    offset = imp["bytes_offset"]
    line_no = imp["lines_read"]
    while not cancel.is_set():
        rows, errors, offset, line_no, eof = await asyncio.to_thread(
            read_batch, imp["path"], offset, line_no, imp["format"], BATCH
        )
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                inserted = 0
                if rows:
                    await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
                    returned = await conn.fetch(
                        INSERT_SQL,
                        [r[0] for r in rows],
                        [r[1] for r in rows],
                        [r[2] for r in rows],
                        [r[3] for r in rows],
                        [r[4] for r in rows],
                        imp["agency"],
                        import_id,
                    )
                    inserted = len(returned)
                if errors:
                    await conn.executemany(ERROR_SQL, [(import_id,) + e for e in errors])
                await conn.execute(
                    PROGRESS_SQL, import_id, offset, line_no, inserted, len(rows) - inserted, len(errors), eof
                )
        if eof:
            return


async def cancel(import_id):
    event = cancels.get(import_id)
    if event:
        event.set()
    task = tasks.get(import_id)
    if task:
        await asyncio.gather(task, return_exceptions=True)
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT status FROM imports WHERE id = $1 FOR UPDATE", import_id)
            if row is None:
                return False
            if row["status"] == "cancelled":
                return True
            tag = await conn.execute(
                "DELETE FROM transactions WHERE import_id = $1 AND manual_edit = false", import_id
            )
            removed = int(tag.split()[-1])
            await conn.execute(
                "UPDATE imports SET status = 'cancelled', rolled_back = $2, finished_at = COALESCE(finished_at, now()) WHERE id = $1",
                import_id,
                removed,
            )
    return True
