import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from agent import desktop, gmail_tools


class GmailAPITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        support = Path(self.enterContext(TemporaryDirectory()))
        (support / "desktop-token").write_text("test-token\n")
        self.enterContext(patch.object(desktop.settings, "APP_SUPPORT", support))

    async def request(self, path, *, method="GET", authorization="Bearer test-token", query=""):
        headers = [] if authorization is None else [(b"authorization", authorization.encode())]
        messages = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)

        await desktop.app({
            "type": "http", "method": method, "path": path, "headers": headers,
            "scheme": "http", "http_version": "1.1", "query_string": urlencode({"q": query}).encode(),
        }, receive, send)
        return messages[0], json.loads(messages[1]["body"])

    async def test_gmail_authorization_requires_bearer_and_reports_provider_failure(self):
        with patch.object(gmail_tools, "authorize", return_value="Complete sign-in in your browser.") as authorize:
            response, _ = await self.request("/gmail/authorize", method="POST", authorization=None)
            self.assertEqual(response["status"], 401)
            authorize.assert_not_called()

            response, body = await self.request("/gmail/authorize", method="POST")
            self.assertEqual(response["status"], 200)
            self.assertEqual(body, {"message": "Complete sign-in in your browser."})
            self.assertIn((b"cache-control", b"no-store"), response["headers"])

            authorize.side_effect = RuntimeError("Gmail sign-in is already in progress.")
            response, body = await self.request("/gmail/authorize", method="POST")
            self.assertEqual(response["status"], 409)
            self.assertEqual(body, {"detail": "Gmail sign-in is already in progress."})

    async def test_inbox_requires_bearer_and_valid_query_and_disables_caching(self):
        rows = [{"id": "message-one", "sender": "sender@example.test", "subject": "Hello", "snippet": "Hi"}]
        with patch.object(gmail_tools, "inbox", return_value=rows) as inbox:
            response, _ = await self.request("/gmail/inbox", authorization="Bearer wrong")
            self.assertEqual(response["status"], 401)
            response, body = await self.request("/gmail/inbox", query="x" * 257)
            self.assertEqual(response["status"], 422)
            self.assertEqual(body, {"detail": "Gmail search must be 256 characters or fewer."})
            inbox.assert_not_called()

            query = "is:unread subject:é"
            response, body = await self.request("/gmail/inbox", query=query)
            self.assertEqual(response["status"], 200)
            self.assertIn((b"cache-control", b"no-store"), response["headers"])
            self.assertEqual(body, {"messages": rows})
            inbox.assert_called_once_with(query)

            inbox.side_effect = RuntimeError("Connect Gmail to load your inbox.")
            response, body = await self.request("/gmail/inbox")
            self.assertEqual(response["status"], 503)
            self.assertIn((b"cache-control", b"no-store"), response["headers"])
            self.assertEqual(body, {"detail": "Connect Gmail to load your inbox."})

    async def test_health_reports_only_sanitized_gmail_status(self):
        with (
            patch.object(gmail_tools, "configured", return_value=True),
            patch.object(gmail_tools, "connected", return_value=False),
            patch.object(gmail_tools, "authorization_error", "Gmail sign-in was canceled."),
            patch.object(desktop.spotify_tools, "configured", return_value=False),
        ):
            response, body = await self.request("/health", authorization=None)
        self.assertEqual(response["status"], 200)
        self.assertIs(body["gmailConfigured"], True)
        self.assertIs(body["gmailConnected"], False)
        self.assertEqual(body["gmailAuthorizationError"], "Gmail sign-in was canceled.")
