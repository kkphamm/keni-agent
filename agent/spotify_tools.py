import json
import logging
import os
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from secrets import token_urlsafe
from threading import Lock, Thread
from time import monotonic, sleep
from urllib.parse import parse_qs, urlsplit

import spotipy
from requests import RequestException
from spotipy.cache_handler import CacheFileHandler
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import SpotifyOAuth

from agent.settings import APP_SUPPORT


SCOPES = "user-read-playback-state user-modify-playback-state"
_CACHE_PATH = APP_SUPPORT / "spotify.json"
_REQUEST_TIMEOUT = 2
_AUTHORIZATION_TIMEOUT = 120
_authorization = Lock()
authorization_error: str | None = None

# Spotipy's debug logs include authorization headers and OAuth payloads.
logging.getLogger("spotipy").setLevel(logging.CRITICAL)


class _TokenCache(CacheFileHandler):
    def save_token_to_cache(self, token_info):
        path = Path(self.cache_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        stream = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        )
        temporary_path = Path(stream.name)
        try:
            with stream:
                json.dump(token_info, stream)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _redirect_uri() -> str:
    return os.getenv("KENI_SPOTIFY_REDIRECT_URI", "").strip() or os.getenv(
        "SPOTIFY_REDIRECT_URI", ""
    ).strip()


def configured() -> bool:
    return bool(
        os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        and os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
        and _redirect_uri()
    )


def _oauth() -> SpotifyOAuth:
    if not configured():
        raise RuntimeError("Set the Spotify client ID, secret, and redirect URI in .env.")
    return SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"].strip(),
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"].strip(),
        redirect_uri=_redirect_uri(),
        scope=SCOPES,
        open_browser=False,
        show_dialog=True,
        cache_handler=_TokenCache(cache_path=_CACHE_PATH),
        requests_timeout=_REQUEST_TIMEOUT,
    )


def connected() -> bool:
    if not configured():
        return False
    token = _TokenCache(cache_path=_CACHE_PATH).get_cached_token()
    if not isinstance(token, dict) or not token.get("access_token"):
        return False
    scope = token.get("scope")
    if not isinstance(scope, str) or not set(SCOPES.split()).issubset(scope.split()):
        return False
    expires_at = token.get("expires_at")
    return isinstance(expires_at, int) and bool(
        token.get("refresh_token") or not SpotifyOAuth.is_token_expired(token)
    )


def authorize() -> str:
    global authorization_error
    oauth = _oauth()
    invalid_redirect = (
        "Register an http://127.0.0.1:PORT/path Spotify redirect and set KENI_SPOTIFY_REDIRECT_URI."
    )
    try:
        redirect = urlsplit(oauth.redirect_uri)
        port = redirect.port
    except ValueError:
        raise RuntimeError(invalid_redirect) from None
    if (
        not port
        or redirect.scheme != "http"
        or redirect.netloc != f"127.0.0.1:{port}"
        or redirect.query
        or redirect.fragment
    ):
        raise RuntimeError(invalid_redirect)
    authorization = _authorization
    if not authorization.acquire(blocking=False):
        return "Finish connecting Spotify in your browser."

    authorization_error = None
    state = token_urlsafe(32)
    finished = False

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(_REQUEST_TIMEOUT)

        def log_message(self, *_args):
            pass

        def do_GET(self):
            nonlocal finished
            global authorization_error
            callback = urlsplit(self.path)
            parameters = parse_qs(callback.query)
            if callback.path != (redirect.path or "/") or parameters.get("state") != [state]:
                self.send_error(400, "Invalid Spotify authorization callback")
                return

            finished = True
            code = parameters.get("code", [])
            status = 400
            if parameters.get("error") == ["access_denied"]:
                message = "Spotify sign-in was canceled. Select Connect Spotify to try again."
            else:
                message = "Spotify returned a sign-in error. Check the registered callback URL and try again."
            if len(code) == 1 and "error" not in parameters:
                try:
                    oauth.get_access_token(code[0], as_dict=False, check_cache=False)
                except (SpotifyOauthError, RequestException, OSError, ValueError, KeyError):
                    message = "Spotify could not connect. Check the Spotify app settings and try again."
                else:
                    status = 200
                    message = "Spotify is connected. You can close this tab and return to Local Voice Agent."

            if status != 200:
                authorization_error = message
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(message.encode())

    try:
        server = HTTPServer(("127.0.0.1", port), Callback)
    except OSError:
        authorization.release()
        raise RuntimeError(
            f"Spotify could not listen on port {port}. Free the port or register a different "
            "loopback redirect in Spotify and set KENI_SPOTIFY_REDIRECT_URI."
        ) from None

    def wait_for_callback():
        global authorization_error
        try:
            with server:
                deadline = monotonic() + _AUTHORIZATION_TIMEOUT
                while not finished:
                    server.timeout = deadline - monotonic()
                    if server.timeout <= 0:
                        authorization_error = "Spotify sign-in expired. Select Connect Spotify to try again."
                        return
                    server.handle_request()
        finally:
            authorization.release()

    try:
        subprocess.run(
            ["/usr/bin/open", oauth.get_authorize_url(state=state)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        Thread(target=wait_for_callback, daemon=True).start()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        server.server_close()
        authorization.release()
        raise RuntimeError("Couldn't open Spotify authorization in your browser. Try again.") from None
    return "Finish connecting Spotify in your browser."


def _client() -> spotipy.Spotify:
    oauth = _oauth()
    try:
        token = oauth.validate_token(oauth.cache_handler.get_cached_token())
    except (SpotifyOauthError, OSError, ValueError, KeyError, TypeError):
        token = None
    if not token or not token.get("access_token"):
        raise RuntimeError("Connect Spotify in the app before requesting playback.")
    return spotipy.Spotify(
        auth=token["access_token"],
        requests_timeout=_REQUEST_TIMEOUT,
        retries=0,
        status_retries=0,
    )


def _search_track(client: spotipy.Spotify, title: str, artist: str) -> dict:
    filters = []
    if title:
        filters.append(f"track:{title}")
    if artist:
        filters.append(f"artist:{artist}")
    search = client.search(q=" ".join(filters), type="track", limit=10) or {}
    tracks = (search.get("tracks") or {}).get("items") or []
    matches = [
        track for track in tracks
        if track.get("uri")
        and track.get("name")
        and track.get("is_playable", True)
        and (not title or track["name"].casefold() == title.casefold())
        and (not artist or any(a["name"].casefold() == artist.casefold() for a in track.get("artists", ())))
    ]
    if not matches:
        if not title:
            raise RuntimeError("Spotify couldn't find a playable track by that exact artist.")
        raise RuntimeError("Spotify couldn't find an exact match for that track.")
    if not artist and len({track["artists"][0]["name"].casefold() for track in matches if track.get("artists")}) > 1:
        raise RuntimeError("Several Spotify tracks have that title. Say the track title and artist.")
    return matches[0]


def _start_local_playback(client: spotipy.Spotify, uri: str) -> str:
    try:
        name = subprocess.run(
            ["/usr/sbin/scutil", "--get", "ComputerName"],
            check=True, capture_output=True, text=True, timeout=3,
        ).stdout.strip().casefold()
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Could not read this Mac’s computer name to select Spotify playback.") from None
    if not name:
        raise RuntimeError("Set this Mac’s computer name in System Settings before playing Spotify.")

    try:
        subprocess.run(
            ["/usr/bin/open", "-g", "-b", "com.spotify.client"],
            check=True, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("macOS couldn’t open the Spotify desktop app. Check that Spotify is installed.") from None

    deadline = monotonic() + 12
    while monotonic() < deadline:
        devices = (client.devices() or {}).get("devices") or []
        matching_ids = {
            device["id"] for device in devices
            if device.get("id") and not device.get("is_restricted")
            and device.get("type") == "Computer"
            and (device.get("name") or "").strip().casefold() == name
        }
        if len(matching_ids) > 1:
            raise RuntimeError("Spotify found multiple computers with this Mac’s name. Give this Mac a unique name.")
        if matching_ids:
            device_id = matching_ids.pop()
            try:
                client.start_playback(device_id=device_id, uris=[uri])
                return device_id
            except SpotifyException as exc:
                if exc.http_status != 404:
                    raise
        sleep(0.75)
    raise RuntimeError("Spotify couldn’t start playback on this Mac. Check that the desktop app is signed in to the same Spotify account.")


def play_track(title: str | None = None, artist: str | None = None) -> str:
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title and not artist:
        raise RuntimeError("Provide a track title or artist for Spotify playback.")

    try:
        client = _client()
        track = _search_track(client, title, artist)
        device_id = _start_local_playback(client, track["uri"])
        for _ in range(4):
            sleep(0.35)
            playback = client.current_playback() or {}
            if (
                playback.get("is_playing")
                and (playback.get("item") or {}).get("uri") == track["uri"]
                and (playback.get("device") or {}).get("id") == device_id
            ):
                artist_name = next(
                    (credit["name"] for credit in track.get("artists", ()) if credit["name"].casefold() == artist.casefold()),
                    None,
                )
                return f"Playing {track['name']}{f' by {artist_name}' if artist_name else ''} on this Mac."
    except SpotifyException as error:
        message = {
            401: "Reconnect Spotify in the app and try again.",
            403: "Spotify denied playback. Check Premium, app access, and playback permissions.",
            404: "Spotify couldn’t complete the playback request for this Mac. Try again.",
            429: "Spotify is temporarily rate limiting requests. Try again later.",
        }.get(error.http_status, "Spotify couldn't complete the playback request. Try again.")
        raise RuntimeError(message) from None
    except RequestException:
        raise RuntimeError("Couldn't reach Spotify to complete the playback request. Try again.") from None
    raise RuntimeError("Spotify accepted the request, but playback couldn't be confirmed.")
