import html
import json
import logging
import os
import re
import stat
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from secrets import compare_digest
from tempfile import NamedTemporaryFile
from threading import Lock, Thread
from time import monotonic
from urllib.parse import parse_qs

import requests
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from oauthlib.oauth2 import OAuth2Error

from agent import settings
from agent.actions import _open


_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_MESSAGES_URI = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
_CLIENT_FILE = "gmail-client.json"
_TOKEN_FILE = "gmail-token.json"
_SIGN_IN = "Finish connecting Gmail in your browser."
_RECONNECT = "Select Connect Gmail to authorize read-only inbox access."
_TIMEOUT = 2.5
_AUTHORIZATION_TIMEOUT = 120
_authorization = Lock()
_token_lock = Lock()
authorization_error: str | None = None

# OAuth debug logs can contain authorization codes and bearer tokens.
for _logger in ("requests_oauthlib", "oauthlib", "google.auth", "urllib3.connectionpool"):
    logging.getLogger(_logger).setLevel(logging.WARNING)


def _private_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid()
                or info.st_size > 65_536):
            raise ValueError("Not a private JSON file")
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("Not a JSON object")
    return data


def _client_config() -> dict:
    try:
        client = _private_json(settings.APP_SUPPORT / _CLIENT_FILE)["installed"]
        if (
            isinstance(client, dict)
            and isinstance(client.get("client_id"), str)
            and re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client["client_id"])
            and isinstance(client.get("client_secret"), str)
            and client["client_secret"]
            and client.get("auth_uri") in {_AUTH_URI, "https://accounts.google.com/o/oauth2/auth"}
            and client.get("token_uri") == _TOKEN_URI
        ):
            return {"client_id": client["client_id"], "client_secret": client["client_secret"],
                    "auth_uri": _AUTH_URI, "token_uri": _TOKEN_URI}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise RuntimeError("Put a private Google Desktop OAuth client JSON in Application Support as gmail-client.json.")


def configured() -> bool:
    try:
        _client_config()
    except RuntimeError:
        return False
    return True


def _cached_credentials() -> Credentials:
    client = _client_config()
    try:
        data = _private_json(settings.APP_SUPPORT / _TOKEN_FILE)
        if (data.get("client_id") != client["client_id"] or data.get("scopes") != [_SCOPE]
                or not isinstance(data.get("refresh_token"), str) or not data["refresh_token"]):
            raise ValueError("Wrong OAuth client or scope")
        # Pass only the expected persisted fields and use the current client secret.
        return Credentials.from_authorized_user_info({
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "refresh_token": data["refresh_token"], "token": data.get("token"), "expiry": data.get("expiry"),
        }, scopes=[_SCOPE])
    except (OSError, ValueError, TypeError, AttributeError):
        raise RuntimeError(_RECONNECT) from None


def connected() -> bool:
    try:
        _cached_credentials()
    except RuntimeError:
        return False
    return True


def _save_credentials(credentials: Credentials) -> None:
    support = settings.APP_SUPPORT
    info = support.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError("Application Support folder must be private")
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=support, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(credentials.to_json(strip=["client_secret"]))
            stream.close()
            temporary.replace(support / _TOKEN_FILE)
        finally:
            temporary.unlink(missing_ok=True)


def authorize() -> str:
    global authorization_error
    client = _client_config()
    if not _authorization.acquire(blocking=False):
        return _SIGN_IN
    authorization_error = None

    def connect() -> None:
        global authorization_error
        finished = False

        class Callback(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(_TIMEOUT)

            def log_message(self, *_args):
                pass

            def do_GET(self):
                nonlocal finished
                global authorization_error
                path, _, query = self.path.partition("?")
                params = parse_qs(query)
                states = params.get("state", [])
                if (path != "/" or self.headers.get("Host") != f"127.0.0.1:{server.server_port}"
                        or len(states) != 1 or not compare_digest(states[0].encode(), state.encode())):
                    self.send_error(400, "Invalid Gmail authorization callback")
                    return

                finished = True
                status = 400
                codes = params.get("code", [])
                if params.get("error") == ["access_denied"]:
                    message = "Gmail sign-in was canceled. Select Connect Gmail to try again."
                else:
                    message = "Gmail could not connect. Check the Google OAuth settings and try again."
                if len(codes) == 1 and "error" not in params:
                    try:
                        flow.fetch_token(code=codes[0], timeout=_TIMEOUT)
                        credentials = flow.credentials
                        if not credentials.refresh_token or set(credentials.granted_scopes or credentials.scopes) != {_SCOPE}:
                            raise ValueError("Expected read-only Gmail access and a refresh token")
                        with _token_lock:
                            _save_credentials(credentials)
                    except (OAuth2Error, requests.RequestException, OSError, ValueError, TypeError, KeyError, Warning):
                        pass
                    else:
                        status = 200
                        message = "Gmail is connected. Return to Local Voice Agent to open your inbox."

                if status != 200:
                    authorization_error = message
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(message.encode())

        try:
            with HTTPServer(("127.0.0.1", 0), Callback) as server:
                flow = Flow.from_client_config({"installed": client}, scopes=[_SCOPE], autogenerate_code_verifier=True)
                flow.oauth2session.max_redirects = 0
                flow.redirect_uri = f"http://127.0.0.1:{server.server_port}/"
                url, state = flow.authorization_url(access_type="offline", prompt="consent")
                _open(url)
                deadline = monotonic() + _AUTHORIZATION_TIMEOUT
                while not finished:
                    server.timeout = deadline - monotonic()
                    if server.timeout <= 0:
                        authorization_error = "Gmail sign-in expired. Select Connect Gmail to try again."
                        return
                    server.handle_request()
        except (OSError, subprocess.SubprocessError, OAuth2Error, ValueError):
            authorization_error = "Could not open Gmail sign-in. Select Connect Gmail to try again."
        finally:
            _authorization.release()

    try:
        Thread(target=connect, daemon=True).start()
    except RuntimeError:
        _authorization.release()
        raise RuntimeError("Could not start Gmail sign-in. Try again.") from None
    return _SIGN_IN


def _forget_token(credentials: Credentials) -> None:
    global authorization_error
    # An old request can fail after a concurrent refresh or account change.
    try:
        cached = _cached_credentials()
        if (cached.refresh_token, cached.token) == (credentials.refresh_token, credentials.token):
            (settings.APP_SUPPORT / _TOKEN_FILE).unlink(missing_ok=True)
            authorization_error = _RECONNECT
    except (OSError, RuntimeError):
        pass


def _inbox_token(rejected: Credentials | None = None) -> Credentials:
    with _token_lock:
        credentials = _cached_credentials()
        if credentials.valid and (rejected is None or credentials.token != rejected.token):
            return credentials
        try:
            with requests.Session() as session:
                session.max_redirects = 0
                request = Request(session=session)
                credentials.refresh(lambda **options: request(**{**options, "timeout": _TIMEOUT}))
            _save_credentials(credentials)
        except RefreshError as error:
            oauth_error = next((arg.get("error") for arg in error.args if isinstance(arg, dict)), None)
            if oauth_error in {"invalid_grant", "invalid_client", "unauthorized_client"}:
                _forget_token(credentials)
                raise RuntimeError(_RECONNECT) from None
            raise RuntimeError("Gmail could not refresh. Try again shortly.") from None
        except (TransportError, OSError, ValueError):
            raise RuntimeError("Gmail could not refresh. Try again shortly.") from None
        return credentials


def inbox(query: str = "") -> list[dict[str, str]]:
    query = query.strip()
    if len(query) > 256:
        raise RuntimeError("Keep the Gmail search to 256 characters or fewer.")
    credentials = _inbox_token()
    retried = False

    def get(url: str, **params) -> dict | None:
        nonlocal credentials, retried
        while True:
            headers = {"Authorization": f"Bearer {credentials.token}"}
            response = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT, allow_redirects=False)
            if response.status_code != 401:
                break
            if retried:
                with _token_lock:
                    _forget_token(credentials)
                raise RuntimeError(_RECONNECT)
            retried = True
            credentials = _inbox_token(rejected=credentials)
        if response.status_code == 404 and url != _MESSAGES_URI:
            return None
        if response.status_code != 200:
            raise RuntimeError("Gmail could not load the inbox. Check that Gmail API is enabled and try again.")
        return response.json()

    try:
        listing = get(_MESSAGES_URI, labelIds="INBOX", maxResults=5, fields="messages(id)", q=query or None)
        messages = []
        for item in (listing or {}).get("messages", [])[:5]:
            message_id = item.get("id", "")
            if not isinstance(message_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id):
                continue
            message = get(f"{_MESSAGES_URI}/{message_id}", format="full", fields="snippet,payload(headers)")
            if message is None:
                continue
            envelope = {h["name"].casefold(): h["value"] for h in message.get("payload", {}).get("headers", [])}
            messages.append({"id": message_id, "sender": envelope.get("from", "")[:250],
                             "subject": envelope.get("subject", "")[:250],
                             "snippet": html.unescape(message.get("snippet", ""))[:350]})
        return messages
    except (requests.RequestException, AttributeError, KeyError, TypeError, ValueError, OSError):
        raise RuntimeError("Gmail could not load the inbox. Try again shortly.") from None
