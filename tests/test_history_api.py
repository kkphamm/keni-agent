from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from agent import desktop, history


class HistoryAPITest(unittest.IsolatedAsyncioTestCase):
    async def get_history(self, authorization=None):
        headers = [] if authorization is None else [(b"authorization", authorization.encode())]
        messages = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)

        await desktop.app({
            "type": "http", "method": "GET", "path": "/history", "headers": headers,
            "scheme": "http", "http_version": "1.1", "query_string": b"",
        }, receive, send)
        return messages[0], json.loads(messages[1]["body"])

    async def test_history_requires_the_desktop_bearer_and_reports_unavailable(self):
        started = datetime(2026, 11, 19, 12, tzinfo=UTC)
        rows = [{
            "id": "0" * 32, "room_name": "keni-test", "action": "vscode", "state": "running",
            "started_at": started, "finished_at": None, "message": None,
        }]
        with TemporaryDirectory() as directory:
            support = Path(directory)
            (support / "desktop-token").write_text("test-token\n")
            with (
                patch.object(desktop.settings, "APP_SUPPORT", support),
                patch.object(history, "list_actions", new_callable=AsyncMock) as read,
            ):
                response, _ = await self.get_history()
                self.assertEqual(response["status"], 401)
                response, _ = await self.get_history("Bearer mismatched-token")
                self.assertEqual(response["status"], 401)
                read.assert_not_awaited()

                read.return_value = rows
                response, body = await self.get_history("Bearer test-token")
                self.assertEqual(response["status"], 200)
                self.assertIn((b"cache-control", b"no-store"), response["headers"])
                expected = {**rows[0], "started_at": "2026-11-19T12:00:00Z"}
                self.assertEqual(body, {"actions": [expected]})

                read.side_effect = history.HistoryUnavailable("Private database details")
                response, body = await self.get_history("Bearer test-token")
                self.assertEqual(response["status"], 503)
                self.assertEqual(body, {"detail": "Action history is unavailable."})
