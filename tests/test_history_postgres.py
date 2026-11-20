from datetime import UTC, datetime, timedelta
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent import history


@unittest.skipUnless(os.getenv("KENI_TEST_DATABASE_URL"), "Requires a migrated PostgreSQL test database")
class HistoryPostgresTest(unittest.IsolatedAsyncioTestCase):
    async def test_saves_separate_outcomes_once_and_preserves_final_states(self):
        room = f"test-{uuid4().hex}"
        succeeded_id, failed_id, running_id = (uuid4().hex for _ in range(3))
        newest = datetime.now(UTC)
        oldest = newest - timedelta(seconds=2)
        middle = newest - timedelta(seconds=1)

        async def save(action_id, action, state, message, started_at):
            await history.save_action(
                room, {"id": action_id, "action": action, "state": state, "message": message}, started_at,
            )

        with patch.dict(os.environ, {"KENI_DATABASE_URL": os.environ["KENI_TEST_DATABASE_URL"]}):
            await save(succeeded_id, "vscode", "running", "Working…", oldest)
            await save(succeeded_id, "vscode", "succeeded", "Opened IRDX in VS Code.", oldest)
            await save(succeeded_id, "vscode", "succeeded", "Duplicate update", oldest)
            await save(failed_id, "spotify", "failed", "No device found.", middle)
            await save(failed_id, "spotify", "running", "Late update", middle)
            await save(running_id, "open_app", "running", "Working…", newest)
            rows = [row for row in await history.list_actions() if row["room_name"] == room]

        self.assertEqual([row["id"] for row in rows], [running_id, failed_id, succeeded_id])
        self.assertEqual([row["action"] for row in rows], ["open_app", "spotify", "vscode"])
        self.assertEqual([row["state"] for row in rows], ["running", "failed", "succeeded"])
        self.assertEqual([row["message"] for row in rows], [None, "No device found.", "Opened IRDX in VS Code."])
        self.assertEqual([row["started_at"] for row in rows], [newest, middle, oldest])
        self.assertIsNone(rows[0]["finished_at"])
        for row in rows[1:]:
            self.assertGreaterEqual(row["finished_at"], row["started_at"])
