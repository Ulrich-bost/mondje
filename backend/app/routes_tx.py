import json
from datetime import date, datetime, timezone
from typing import Annotated, Literal, Optional
from uuid import UUID, uuid4

import orjson
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .importer import LOCK_KEY
from .parsing import MAX_AMOUNT

router = APIRouter()

COLS = "seq, id, type, amount, description, agency, created_at, updated_at, deleted_at, manual_edit, import_id, source_line"
Amount = Annotated[int, Field(strict=True, gt=0, le=MAX_AMOUNT)]


class TxIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Optional[UUID] = None
    type: Literal["CREDIT", "DEBIT"]
    amount: Amount
    description: str = Field(default="", max_length=255)
    agency: str = Field(default="", max_length=60)


class TxPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Optional[Amount] = None
    description: Optional[str] = Field(default=None, max_length=255)


@router.post("/transactions", status_code=201)
async def create_tx(body: TxIn):
    if "\x00" in body.description:
        raise HTTPException(422, "description invalide : caractère nul interdit")
    tx_id = body.id or uuid4()
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
            row = await conn.fetchrow(
                f"INSERT INTO transactions (id, type, amount, description, agency) VALUES ($1, $2, $3, $4, $5) "
                f"ON CONFLICT (id) DO NOTHING RETURNING {COLS}",
                tx_id, body.type, body.amount, body.description.strip(), body.agency,
            )
    if row is None:
        raise HTTPException(409, "cet UUID existe déjà")
    return dict(row)


@router.get("/transactions")
async def list_tx(
    tx_type: Optional[Literal["CREDIT", "DEBIT"]] = Query(None, alias="type"),
    min_amount: Optional[int] = Query(None, ge=1),
    max_amount: Optional[int] = Query(None, ge=1),
    q: Optional[str] = Query(None, max_length=100),
    agency: Optional[str] = Query(None, max_length=60),
    deleted: bool = False,
    cursor: Optional[int] = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    params = []

    def add(value):
        params.append(value)
        return f"${len(params)}"

    conds = ["deleted_at IS NOT NULL AND deleted_at > now() - interval '24 hours'"] if deleted else ["deleted_at IS NULL"]
    if tx_type:
        conds.append(f"type = {add(tx_type)}")
    if min_amount is not None:
        conds.append(f"amount >= {add(min_amount)}")
    if max_amount is not None:
        conds.append(f"amount <= {add(max_amount)}")
    if q:
        conds.append(f"search @@ plainto_tsquery('simple', {add(q)})")
    if agency is not None:
        conds.append(f"agency = {add(agency)}")
    if cursor is not None:
        conds.append(f"seq < {add(cursor)}")
    params.append(limit + 1)
    order = "seq + 0" if min_amount is not None or max_amount is not None else "seq"
    inner = f"SELECT seq FROM transactions WHERE {' AND '.join(conds)} ORDER BY {order} DESC LIMIT ${len(params)}"
    if deleted:
        sql = f"SELECT {COLS} FROM transactions WHERE {' AND '.join(conds)} ORDER BY seq DESC LIMIT ${len(params)}"
    else:
        sql = f"SELECT {COLS} FROM transactions WHERE deleted_at IS NULL AND seq = ANY(ARRAY({inner})) ORDER BY seq DESC"
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    more = len(rows) > limit
    items = [dict(r) for r in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["seq"] if more else None}


@router.get("/transactions/{tx_id}")
async def get_tx(tx_id: UUID):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {COLS} FROM transactions WHERE id = $1", tx_id)
    if row is None:
        raise HTTPException(404, "transaction introuvable")
    return dict(row)


@router.patch("/transactions/{tx_id}")
async def patch_tx(tx_id: UUID, body: TxPatch):
    if body.amount is None and body.description is None:
        raise HTTPException(422, "rien à modifier : fournir amount et/ou description")
    description = body.description.strip() if body.description is not None else None
    if description is not None and "\x00" in description:
        raise HTTPException(422, "description invalide : caractère nul interdit")
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE transactions SET amount = COALESCE($2, amount), description = COALESCE($3, description), "
            f"updated_at = now(), manual_edit = true WHERE id = $1 AND deleted_at IS NULL RETURNING {COLS}",
            tx_id, body.amount, description,
        )
    if row is None:
        raise HTTPException(404, "transaction introuvable")
    return dict(row)


@router.delete("/transactions/{tx_id}")
async def delete_tx(tx_id: UUID):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE transactions SET deleted_at = now(), manual_edit = true "
            "WHERE id = $1 AND deleted_at IS NULL "
            "RETURNING id, deleted_at, deleted_at + interval '24 hours' AS restorable_until",
            tx_id,
        )
    if row is None:
        raise HTTPException(404, "transaction introuvable")
    return dict(row)


@router.post("/transactions/{tx_id}/restore")
async def restore_tx(tx_id: UUID):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE transactions SET deleted_at = NULL WHERE id = $1 "
            f"AND deleted_at > now() - interval '24 hours' RETURNING {COLS}",
            tx_id,
        )
    if row is None:
        raise HTTPException(404, "transaction introuvable ou délai de 24 heures dépassé")
    return dict(row)


@router.get("/transactions/{tx_id}/source")
async def tx_source(tx_id: UUID):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT t.id, t.import_id, t.source_line, i.filename, i.agency FROM transactions t "
            "LEFT JOIN imports i ON i.id = t.import_id WHERE t.id = $1",
            tx_id,
        )
    if row is None:
        raise HTTPException(404, "transaction introuvable")
    result = dict(row)
    result["origin"] = "import" if row["import_id"] else "saisie manuelle"
    return result


@router.get("/balance")
async def balance(at: Optional[date] = None, agency: Optional[str] = Query(None, max_length=60)):
    day = at or datetime.now(timezone.utc).date()
    sql = "SELECT COALESCE(SUM(net), 0)::bigint FROM daily_balance WHERE day <= $1"
    params = [day]
    if agency is not None:
        sql += " AND agency = $2"
        params.append(agency)
    async with db.pool.acquire() as conn:
        value = await conn.fetchval(sql, *params)
    return {"at": day, "balance": value}


@router.get("/balance/agencies")
async def balance_agencies(at: Optional[date] = None):
    day = at or datetime.now(timezone.utc).date()
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT agency, SUM(net)::bigint AS balance FROM daily_balance WHERE day <= $1 GROUP BY agency ORDER BY agency",
            day,
        )
    return {"at": day, "items": [dict(r) for r in rows]}


EXPORT_SQL = (
    "SELECT id, type, amount, description, agency, created_at FROM transactions "
    "WHERE deleted_at IS NULL ORDER BY seq"
)
CSV_HEADER = b"id,type,amount,description,agency,created_at\n"


def csv_cell(value):
    text = str(value)
    if any(ch in text for ch in ',"\n\r'):
        return '"' + text.replace('"', '""') + '"'
    return text


async def export_stream(fmt):
    async with db.pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            if fmt == "csv":
                yield CSV_HEADER
            buffer = []
            async for r in conn.cursor(EXPORT_SQL, prefetch=5000):
                created = r["created_at"].isoformat()
                if fmt == "json":
                    buffer.append(orjson.dumps({
                        "id": str(r["id"]), "type": r["type"], "amount": r["amount"],
                        "description": r["description"], "agency": r["agency"], "created_at": created,
                    }))
                else:
                    buffer.append((",".join((
                        str(r["id"]), r["type"], str(r["amount"]), csv_cell(r["description"]),
                        csv_cell(r["agency"]), created,
                    ))).encode())
                if len(buffer) >= 5000:
                    yield b"\n".join(buffer) + b"\n"
                    buffer = []
            if buffer:
                yield b"\n".join(buffer) + b"\n"


@router.get("/export.json")
async def export_json():
    return StreamingResponse(
        export_stream("json"),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="mondje-export.json"'},
    )


@router.get("/export.csv")
async def export_csv():
    return StreamingResponse(
        export_stream("csv"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="mondje-export.csv"'},
    )
