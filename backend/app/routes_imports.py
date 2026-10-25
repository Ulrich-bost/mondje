import csv
import hashlib
import io
import json
import os
import uuid
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import db, importer
from .parsing import looks_like_json_array

router = APIRouter()
UPLOADS = Path(os.getenv("UPLOAD_DIR", "/data/uploads"))

IMPORT_COLS = (
    "id, filename, format, agency, sha256, size_bytes, status, bytes_offset, lines_read, inserted, "
    "duplicates, errors, rolled_back, failure, created_at, finished_at, "
    "(EXTRACT(EPOCH FROM (COALESCE(finished_at, now()) - created_at)) * 1000)::float8 AS duration_ms"
)


async def fetch_import(import_id):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {IMPORT_COLS} FROM imports WHERE id = $1", import_id)
    if row is None:
        raise HTTPException(404, "import introuvable")
    return dict(row)


@router.post("/imports", status_code=202)
async def upload(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=255),
    agency: str = Query("", max_length=60),
):
    extension = Path(filename).suffix.lower()
    fmt = {".txt": "txt", ".json": "json"}.get(extension)
    if fmt is None:
        raise HTTPException(422, "extension non supportée : .txt ou .json attendu")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    import_id = uuid.uuid4()
    part = UPLOADS / f"{import_id}.part"
    final = UPLOADS / f"{import_id}{extension}"
    digest = hashlib.sha256()
    size = 0
    head = b""
    try:
        with open(part, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                if len(head) < 4096:
                    head = (head + chunk)[:4096]
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if size == 0:
        part.unlink(missing_ok=True)
        raise HTTPException(422, "fichier vide")
    if fmt == "json" and looks_like_json_array(head):
        part.unlink(missing_ok=True)
        raise HTTPException(
            422,
            "un tableau JSON unique est refusé : fournir une transaction par ligne, chaque ligne étant un objet JSON complet (NDJSON)",
        )
    os.replace(part, final)
    sha = digest.hexdigest()
    async with db.pool.acquire() as conn:
        created = await conn.fetchval(
            "INSERT INTO imports (id, filename, format, agency, sha256, path, size_bytes) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (sha256) WHERE status IN ('running', 'done') DO NOTHING RETURNING id",
            import_id, filename, fmt, agency, sha, str(final), size,
        )
        if created is None:
            final.unlink(missing_ok=True)
            existing = await conn.fetchval(
                "SELECT id FROM imports WHERE sha256 = $1 AND status IN ('running', 'done')", sha
            )
    if created is None:
        result = await fetch_import(existing)
        result["already_known"] = True
        return result
    importer.launch(import_id)
    result = await fetch_import(import_id)
    result["already_known"] = False
    return result


@router.get("/imports")
async def list_imports(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {IMPORT_COLS} FROM imports ORDER BY created_at DESC LIMIT $1 OFFSET $2", limit + 1, offset
        )
    return {"items": [dict(r) for r in rows[:limit]], "next_offset": offset + limit if len(rows) > limit else None}


@router.get("/imports/{import_id}")
async def get_import(import_id: UUID):
    return await fetch_import(import_id)


@router.post("/imports/{import_id}/cancel")
async def cancel_import(import_id: UUID):
    await fetch_import(import_id)
    await importer.cancel(import_id)
    return await fetch_import(import_id)


@router.get("/imports/{import_id}/errors")
async def import_errors(
    import_id: UUID,
    cursor: Optional[int] = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    await fetch_import(import_id)
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, line_no, col, content, reason FROM import_errors "
            "WHERE import_id = $1 AND id > $2 ORDER BY id LIMIT $3",
            import_id, cursor or 0, limit + 1,
        )
    items = [dict(r) for r in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}


def numbered(raw_lines, first_no):
    return [
        {"n": first_no + i, "text": line.decode("utf-8", "replace").rstrip("\r").lstrip("\ufeff")}
        for i, line in enumerate(raw_lines)
    ]


@router.get("/imports/{import_id}/errors/{error_id}/context")
async def error_context(import_id: UUID, error_id: int, around: int = Query(3, ge=0, le=20)):
    async with db.pool.acquire() as conn:
        err = await conn.fetchrow(
            "SELECT e.line_no, e.byte_start, e.byte_end, e.col, e.reason, i.path "
            "FROM import_errors e JOIN imports i ON i.id = e.import_id WHERE e.id = $1 AND e.import_id = $2",
            error_id, import_id,
        )
    if err is None:
        raise HTTPException(404, "erreur introuvable")
    window_start = max(0, err["byte_start"] - 8192)
    with open(err["path"], "rb") as handle:
        handle.seek(window_start)
        data = handle.read(err["byte_end"] - window_start + 8192)
    cut_start = err["byte_start"] - window_start
    cut_end = err["byte_end"] - window_start
    before = data[:cut_start].split(b"\n")[:-1]
    if window_start > 0 and before:
        before = before[1:]
    before = before[len(before) - around:] if around else []
    after = data[cut_end:].split(b"\n")[:around] if around else []
    target = numbered([data[cut_start:cut_end].rstrip(b"\r\n")], err["line_no"])[0]
    return {
        "line_no": err["line_no"],
        "col": err["col"],
        "reason": err["reason"],
        "byte_start": err["byte_start"],
        "byte_end": err["byte_end"],
        "before": numbered(before, err["line_no"] - len(before)),
        "line": target,
        "after": numbered(after, err["line_no"] + 1),
    }


def split_row(fmt, content):
    if fmt == "json":
        try:
            obj = json.loads(content)
            if isinstance(obj, dict):
                return [str(obj.get(k, "")) for k in ("id", "type", "amount", "description")]
        except ValueError:
            pass
        return [content, "", "", ""]
    parts = content.split(";")
    if len(parts) > 4:
        parts = parts[:3] + [";".join(parts[3:])]
    return parts + [""] * (4 - len(parts))


@router.get("/imports/{import_id}/errors.csv")
async def errors_csv(import_id: UUID):
    imp = await fetch_import(import_id)

    async def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["id", "type", "amount", "description", "reason"])
        yield buffer.getvalue().encode()
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                count = 0
                async for r in conn.cursor(
                    "SELECT content, reason FROM import_errors WHERE import_id = $1 ORDER BY id", import_id, prefetch=2000
                ):
                    writer.writerow(split_row(imp["format"], r["content"]) + [r["reason"]])
                    count += 1
                    if count % 2000 == 0:
                        yield buffer.getvalue().encode()
                        buffer = io.StringIO()
                        writer = csv.writer(buffer)
                yield buffer.getvalue().encode()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="erreurs-{import_id}.csv"'},
    )


@router.get("/imports/{import_id}/transactions")
async def import_transactions(
    import_id: UUID,
    cursor: Optional[int] = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    imp = await fetch_import(import_id)
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, type, amount, description, source_line, manual_edit, deleted_at FROM transactions "
            "WHERE import_id = $1 AND source_line > $2 ORDER BY source_line LIMIT $3",
            import_id, cursor or 0, limit + 1,
        )
    items = [dict(r) | {"filename": imp["filename"]} for r in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["source_line"] if len(rows) > limit else None}
