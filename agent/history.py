import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import os

import psycopg
from psycopg.rows import DictRow, dict_row


class HistoryUnavailable(RuntimeError):
    """History could not be read or saved."""


@asynccontextmanager
async def _connect() -> AsyncIterator[psycopg.AsyncConnection[DictRow]]:
    url = os.environ.get("KENI_DATABASE_URL")
    if not url:
        raise HistoryUnavailable("Action history is unavailable.")

    try:
        async with asyncio.timeout(1.5):
            async with await psycopg.AsyncConnection.connect(
                url,
                autocommit=True,
                connect_timeout=2,
                options="-c statement_timeout=750 -c lock_timeout=300",
                row_factory=dict_row,
            ) as connection:
                yield connection
    except (psycopg.Error, TimeoutError, ValueError):
        raise HistoryUnavailable("Action history is unavailable.") from None


async def save_action(room_name: str, event: dict[str, str], started_at: datetime) -> None:
    running = event["state"] == "running"
    finished_at = None if running else datetime.now(UTC)
    async with _connect() as connection:
        await connection.execute(
            """
            INSERT INTO public.action_runs (id, room_name, action_type, state, started_at, finished_at, summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                state = EXCLUDED.state,
                finished_at = EXCLUDED.finished_at,
                summary = EXCLUDED.summary
            WHERE action_runs.state = 'running' AND EXCLUDED.state <> 'running'
            """,
            (
                event["id"], room_name, event["action"], event["state"], started_at,
                finished_at,
                None if running else event["message"][:512],
            ),
        )


async def list_actions() -> list[DictRow]:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT replace(id::text, '-', '') AS id, room_name, action_type AS action,
                   state, started_at, finished_at, summary AS message
            FROM public.action_runs
            ORDER BY started_at DESC, action_runs.id DESC
            LIMIT 50
            """,
        )
        return await cursor.fetchall()
