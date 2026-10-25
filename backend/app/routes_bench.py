from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import db

router = APIRouter()


class BenchResult(BaseModel):
    challenge: int
    label: str = Field(max_length=200)
    duration_ms: Optional[float] = None
    rows_ok: Optional[int] = None
    errors_reported: Optional[int] = None
    detail: Optional[str] = Field(default=None, max_length=2000)


@router.post("/bench/results", status_code=201)
async def save_result(body: BenchResult):
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO bench_results (challenge, label, duration_ms, rows_ok, errors_reported, detail) "
            "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (challenge) DO UPDATE SET "
            "label = EXCLUDED.label, duration_ms = EXCLUDED.duration_ms, rows_ok = EXCLUDED.rows_ok, "
            "errors_reported = EXCLUDED.errors_reported, detail = EXCLUDED.detail, measured_at = now()",
            body.challenge, body.label, body.duration_ms, body.rows_ok, body.errors_reported, body.detail,
        )
    return {"ok": True}


@router.get("/bench/results")
async def list_results():
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT challenge, label, duration_ms, rows_ok, errors_reported, detail, measured_at "
            "FROM bench_results ORDER BY challenge LIMIT 200"
        )
    return {"items": [dict(r) for r in rows]}
