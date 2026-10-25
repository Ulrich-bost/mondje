import os
from pathlib import Path

import asyncpg

pool = None


async def init():
    global pool
    pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"],
        min_size=2,
        max_size=int(os.getenv("POOL_MAX", "20")),
    )
    async with pool.acquire() as conn:
        await conn.execute((Path(__file__).parent / "schema.sql").read_text())


async def close():
    await pool.close()
