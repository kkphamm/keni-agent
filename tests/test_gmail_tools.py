import json
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from agent import gmail_tools as gmail


class GmailTest(unittest.TestCase):
    def setUp(self):
        self.support = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(gmail.settings, "APP_SUPPORT", self.support))
        self.enterContext(patch.object(gmail, "authorization_error", None))
        self.client = {
            "client_id": "fixture.apps.googleusercontent.com",
            "client_secret": "fixture-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        self.client_file = self.support / "gmail-client.json"
        self.client_file.write_text(json.dumps({"installed": self.client}), encoding="utf-8")
        self.client_file.chmod(0o600)
        self.credentials = Credentials(
            "fixture-access-token",
            refresh_token="fixture-refresh-token",
            token_uri=self.client["token_uri"],
            client_id=self.client["client_id"],
            client_secret=self.client["client_secret"],
            scopes=[gmail._SCOPE],
            expiry=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
        )

    def test_rejects_public_files_and_unknown_oauth_endpoints(self):
        self.assertTrue(gmail.configured())
        self.client_file.chmod(0o644)
        self.assertFalse(gmail.configured())
        self.client_file.chmod(0o600)
        self.client["token_uri"] = "https://example.org/token"
        self.client_file.write_text(json.dumps({"installed": self.client}), encoding="utf-8")
        self.assertFalse(gmail.configured())

    def test_pkce_callback_checks_state_before_writing_private_token(self):
        opened = Queue()

        def exchange(flow, **options):
            self.assertGreaterEqual(len(flow.code_verifier), 43)
            self.assertEqual(options, {"code": "fixture-code", "timeout": gmail._TIMEOUT})
            flow.oauth2session.token = {
                "access_token": self.credentials.token, "refresh_token": self.credentials.refresh_token,
                "expires_at": self.credentials.expiry.replace(tzinfo=UTC).timestamp(),
                "scope": [gmail._SCOPE],
            }

        with patch.object(gmail, "_open", side_effect=opened.put), patch.object(
            gmail.Flow, "fetch_token", autospec=True, side_effect=exchange
        ) as fetch:
            self.assertIn("in your browser", gmail.authorize())
            url = urlsplit(opened.get(timeout=3))
            params = parse_qs(url.query)
            self.assertEqual(url.hostname, "accounts.google.com")
            self.assertEqual(params["scope"], [gmail._SCOPE])
            self.assertEqual(params["code_challenge_method"], ["S256"])
            redirect = urlsplit(params["redirect_uri"][0])
            self.assertEqual(redirect.hostname, "127.0.0.1")
            with closing(HTTPConnection(redirect.hostname, redirect.port, timeout=3)) as connection:
                connection.request("GET", "/?state=incorrect&code=fixture-code")
                with connection.getresponse() as response:
                    self.assertEqual(response.status, 400)
                    response.read()
                fetch.assert_not_called()
                query = urlencode({"state": params["state"][0], "code": "fixture-code"})
                connection.request("GET", "/?" + query)
                with connection.getresponse() as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"Gmail is connected", response.read())
        self.assertTrue(gmail.connected())
        token_file = self.support / "gmail-token.json"
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("client_secret", json.loads(token_file.read_text(encoding="utf-8")))

    def test_inbox_search_requests_only_preview_fields(self):
        gmail._save_credentials(self.credentials)
        with patch.object(gmail.requests, "get", side_effect=[
            SimpleNamespace(status_code=200, json=lambda: {"messages": [{"id": "abc"}]}),
            SimpleNamespace(status_code=200, json=lambda: {
                "snippet": "one &amp; two", "payload": {"headers": [
                    {"name": "From", "value": "Keni fixture"}, {"name": "Subject", "value": "Test"},
                ]},
            }),
        ]) as request:
            self.assertEqual(gmail.inbox(" from:example.org "), [
                {"id": "abc", "sender": "Keni fixture", "subject": "Test", "snippet": "one & two"},
            ])
        listing, preview = request.call_args_list
        self.assertEqual(listing.kwargs["params"], {
            "labelIds": "INBOX", "maxResults": 5, "fields": "messages(id)", "q": "from:example.org",
        })
        self.assertEqual(preview.kwargs["params"]["fields"], "snippet,payload(headers)")
        self.assertTrue(all(not call.kwargs["allow_redirects"] for call in request.call_args_list))

    def test_revoked_refresh_token_requires_reconnection(self):
        expired = self.credentials
        expired.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        gmail._save_credentials(expired)
        with patch.object(Credentials, "refresh", side_effect=RefreshError("fixture", {"error": "invalid_grant"})):
            with self.assertRaisesRegex(RuntimeError, "Connect Gmail"):
                gmail.inbox()
        self.assertFalse(gmail.connected())

    def test_inbox_refreshes_once_if_the_access_token_is_rejected(self):
        gmail._save_credentials(self.credentials)

        def refresh(credentials, _request):
            credentials.token = "fixture-renewed-access-token"
            credentials.expiry = self.credentials.expiry

        with (
            patch.object(Credentials, "refresh", autospec=True, side_effect=refresh) as retry,
            patch.object(gmail.requests, "get", side_effect=[
                SimpleNamespace(status_code=401),
                SimpleNamespace(status_code=200, json=lambda: {}),
            ]) as request,
        ):
            self.assertEqual(gmail.inbox(), [])
        retry.assert_called_once()
        first, second = request.call_args_list
        self.assertEqual(first.kwargs["headers"]["Authorization"], "Bearer fixture-access-token")
        self.assertEqual(second.kwargs["headers"]["Authorization"], "Bearer fixture-renewed-access-token")
        self.assertTrue(gmail.connected())
