import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from fcntl import LOCK_EX, flock
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


_PLAYBACK_SCOPES = "user-read-playback-state user-modify-playback-state"
_PLAYLIST_SCOPE = "playlist-read-private"
_TOP_TRACKS_SCOPE = "user-top-read"
SCOPES = f"{_PLAYBACK_SCOPES} {_PLAYLIST_SCOPE} {_TOP_TRACKS_SCOPE}"
_CACHE_PATH = APP_SUPPORT / "spotify.json"
_LINKS_PATH = APP_SUPPORT / "spotify-playlists.json"
_REQUEST_TIMEOUT = 2
_AUTHORIZATION_TIMEOUT = 120
_authorization = Lock()
authorization_error: str | None = None

# Spotipy's debug logs include authorization headers and OAuth payloads.
logging.getLogger("spotipy").setLevel(logging.CRITICAL)


def _write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    stream = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temporary_path = Path(stream.name)
    try:
        with stream:
            json.dump(data, stream)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _lock(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as handle:
        flock(handle, LOCK_EX)
        yield


class _TokenCache(CacheFileHandler):
    def save_token_to_cache(self, token_info):
        _write_private_json(Path(self.cache_path), token_info)


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


def _scopes(token: object) -> set[str]:
    scope = token.get("scope") if isinstance(token, dict) else None
    return set(scope.split()) if isinstance(scope, str) else set()


def connected() -> bool:
    if not configured():
        return False
    token = _TokenCache(cache_path=_CACHE_PATH).get_cached_token()
    required = {*_PLAYBACK_SCOPES.split(), _PLAYLIST_SCOPE}
    if not required.issubset(_scopes(token)) or not token.get("access_token"):
        return False
    expires_at = token.get("expires_at")
    return isinstance(expires_at, int) and bool(
        token.get("refresh_token") or not SpotifyOAuth.is_token_expired(token)
    )


def top_tracks_authorized() -> bool:
    return configured() and _TOP_TRACKS_SCOPE in _scopes(_TokenCache(cache_path=_CACHE_PATH).get_cached_token())


def authorization_pending() -> bool:
    return _authorization.locked()


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
                    with _lock(_CACHE_PATH):
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


def disconnect() -> str:
    global authorization_error
    if not _authorization.acquire(blocking=False):
        raise RuntimeError("Finish or cancel the Spotify sign-in in your browser first.")
    try:
        with _lock(_CACHE_PATH):
            _CACHE_PATH.unlink(missing_ok=True)
        authorization_error = None
    except OSError:
        raise RuntimeError("Couldn’t remove the local Spotify sign-in. Try again.") from None
    finally:
        _authorization.release()
    return "Signed out of Spotify in Keni."


def _client(*, scope: str | None = None) -> spotipy.Spotify:
    oauth = _oauth()
    try:
        with _lock(_CACHE_PATH):
            cached = oauth.cache_handler.get_cached_token()
            granted = _scopes(cached)
            required = set(_PLAYBACK_SCOPES.split())
            if required.issubset(granted) and scope and scope not in granted:
                if scope == _PLAYLIST_SCOPE:
                    raise RuntimeError("Connect Spotify in the app and grant playlist access.")
                raise RuntimeError("Use Reauthorize in Keni’s Spotify panel to grant access to your top tracks.")
            # Spotipy writes this scope into the refreshed cache. Preserve all granted permissions.
            oauth.scope = " ".join(sorted(granted | required))
            token = oauth.validate_token(cached)
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


def _valid_uri(uri: object, kind: str) -> bool:
    return isinstance(uri, str) and re.fullmatch(rf"spotify:{kind}:[A-Za-z0-9]+", uri) is not None


def _name_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _search_tracks(client: spotipy.Spotify, query: str) -> list[dict]:
    result = client.search(q=query, type="track", limit=10) or {}
    return (result.get("tracks") or {}).get("items") or []


def _playable(track: dict) -> bool:
    return bool(track and _valid_uri(track.get("uri"), "track") and track.get("is_playable", True))


def _credits(track: dict) -> list[str]:
    return [credit["name"] for credit in (track.get("artists") or []) if credit and credit.get("name")]


def _search_track(client: spotipy.Spotify, title: str, artist: str) -> dict:
    filters = []
    if title:
        filters.append(f"track:{title}")
    if artist:
        filters.append(f"artist:{artist}")
    matches = [
        track for track in _search_tracks(client, " ".join(filters))
        if _playable(track)
        and track.get("name")
        and (not title or _name_key(track["name"]) == _name_key(title))
        and (not artist or any(_name_key(credit) == _name_key(artist) for credit in _credits(track)))
    ]
    if not matches:
        if not title:
            raise RuntimeError("Spotify couldn't find a playable track by that exact artist.")
        raise RuntimeError("Spotify couldn't find an exact match for that track.")
    primary_artists = {_name_key(credits[0]) for track in matches if (credits := _credits(track))}
    if not artist and len(primary_artists) > 1:
        raise RuntimeError("Several Spotify tracks have that title. Say the track title and artist.")
    return matches[0]


def _library_playlists(client: spotipy.Spotify) -> dict[str, str]:
    playlists: dict[str, str] = {}
    page_size = 50
    for offset in range(0, 100_001, page_size):
        page = client.current_user_playlists(limit=page_size, offset=offset) or {}
        for item in page.get("items") or []:
            if isinstance(item, dict) and _valid_uri(item.get("uri"), "playlist") and item.get("name"):
                playlists[item["uri"]] = item["name"]
        if not page.get("next"):
            return playlists
    raise RuntimeError("Spotify couldn't finish searching your playlists.")


def _find_playlist(client: spotipy.Spotify, query: str) -> tuple[str, str]:
    playlists = _library_playlists(client)
    exact = {uri: name for uri, name in playlists.items() if _name_key(name) == query}
    if len(exact) > 1:
        raise RuntimeError(
            "Several Spotify playlists have that exact name. Give the intended playlist a unique name in Spotify."
        )
    matches = exact or {uri: name for uri, name in playlists.items() if query in _name_key(name)}
    if len(matches) > 1:
        raise RuntimeError("Several Spotify playlists match. Say the full playlist name.")
    if not matches:
        raise RuntimeError("Spotify couldn't find a matching playlist in your library.")
    return next(iter(matches.items()))


def _playlist_links() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_LINKS_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise RuntimeError("Keni couldn’t read its linked Spotify playlists.") from None
    if not isinstance(data, dict) or any(
        not isinstance(links, dict) or any(
            not _valid_uri(uri, "playlist") or not isinstance(name, str) or not name.strip()
            for uri, name in links.items()
        )
        for links in data.values()
    ):
        raise RuntimeError("Keni couldn’t read its linked Spotify playlists.")
    return data


def _profile(client: spotipy.Spotify) -> dict:
    profile = client.current_user() or {}
    if not profile.get("id"):
        raise RuntimeError("Spotify couldn’t identify the signed-in account.")
    return profile


@dataclass(frozen=True)
class _Playback:
    label: str
    uris: tuple[str, ...] = ()
    context_uri: str | None = None
    shuffle: bool = False

    def matches(self, state: dict) -> bool:
        if self.context_uri:
            return (state.get("context") or {}).get("uri") == self.context_uri
        uri = (state.get("item") or {}).get("uri")
        return uri in self.uris if self.shuffle else uri == self.uris[0]


def _linked_playlist(client: spotipy.Spotify, query: str) -> _Playback | None:
    key = _name_key(query)
    accounts = _playlist_links()
    if not any(_name_key(name) == key for links in accounts.values() for name in links.values()):
        return None
    links = accounts.get(_profile(client)["id"], {})
    for uri, name in links.items():
        if _name_key(name) == key:
            return _Playback(f"playlist {name}", context_uri=uri, shuffle=True)
    return None


def _start_on_device(client: spotipy.Spotify, device_id: str, plan: _Playback) -> _Playback:
    if not plan.shuffle:
        try:
            client.shuffle(state=False, device_id=device_id)
        except SpotifyException as exc:
            if exc.http_status not in (403, 404):
                raise
            # A newly opened player may need an active track before it accepts shuffle changes.
            client.start_playback(device_id=device_id, uris=[plan.uris[0]])
            client.shuffle(state=False, device_id=device_id)

    if plan.context_uri:
        try:
            client.start_playback(device_id=device_id, context_uri=plan.context_uri)
        except SpotifyException as exc:
            if not plan.uris or exc.http_status not in (403, 404):
                raise
            plan = replace(plan, context_uri=None)
    if not plan.context_uri:
        client.start_playback(device_id=device_id, uris=list(plan.uris))
    if plan.shuffle:
        client.shuffle(state=True, device_id=device_id)
    return plan


def _start_local_playback(client: spotipy.Spotify, plan: _Playback) -> tuple[str, _Playback]:
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
            ["/usr/bin/open", "-g", "-j", "-b", "com.spotify.client"],
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
                return device_id, _start_on_device(client, device_id, plan)
            except SpotifyException as exc:
                if exc.http_status != 404:
                    raise
        sleep(0.75)
    raise RuntimeError("Spotify couldn’t start playback on this Mac. Check that the desktop app is signed in to the same Spotify account.")


@contextmanager
def _request(operation: str):
    try:
        yield
    except SpotifyException as error:
        message = {
            401: "Reconnect Spotify in the app and try again.",
            403: f"Spotify denied {operation}. Check your account and app permissions.",
            404: f"Spotify couldn’t complete {operation}. Try again.",
            429: "Spotify is temporarily rate limiting requests. Try again later.",
        }.get(error.http_status, f"Spotify couldn't complete {operation}. Try again.")
        raise RuntimeError(message) from None
    except RequestException:
        raise RuntimeError(f"Couldn't reach Spotify to complete {operation}. Try again.") from None


def _play(resolve: Callable[[spotipy.Spotify], _Playback], *, scope: str | None = None) -> str:
    with _request("playback"):
        client = _client(scope=scope)
        plan = resolve(client)
        with _lock(_CACHE_PATH.with_stem("spotify-playback")):
            device_id, plan = _start_local_playback(client, plan)
            for _ in range(4):
                sleep(0.35)
                state = client.current_playback() or {}
                if (
                    state.get("is_playing")
                    and (state.get("device") or {}).get("id") == device_id
                    and plan.matches(state)
                    and state.get("shuffle_state", plan.shuffle) == plan.shuffle
                ):
                    message = f"Playing {plan.label} on this Mac."
                    if plan.shuffle:
                        return message
                    if len(plan.uris) == 1:
                        return f"{message} No follow-up tracks were available."
                    if state.get("repeat_state") == "track":
                        client.repeat(state="off", device_id=device_id)
                    count = len(plan.uris) - 1
                    return f"{message} {count} more {'track' if count == 1 else 'tracks'} queued."
    raise RuntimeError("Spotify accepted the request, but playback couldn't be confirmed.")


def _recommendations(client: spotipy.Spotify, seeds: tuple[str, ...]) -> list[dict]:
    try:
        result = client.recommendations(seed_tracks=[uri.split(":")[-1] for uri in seeds[:5]], limit=30) or {}
        return result.get("tracks") or []
    except (SpotifyException, RequestException):
        return []


def _track_uris(tracks: list[dict]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(track["uri"] for track in tracks if _playable(track)))


def _track_queue(client: spotipy.Spotify, track: dict, artist: str = "") -> _Playback:
    uri = track["uri"]
    credits = _credits(track)
    artist_name = next((name for name in credits if _name_key(name) == _name_key(artist)), None)
    artist_name = artist_name or next(iter(credits), "")
    title = track["name"]

    def follow_ups(tracks: list[dict]) -> tuple[str, ...]:
        return _track_uris([
            candidate for candidate in tracks
            if candidate and candidate.get("uri") != uri
            and _name_key(candidate.get("name") or "") != _name_key(title)
        ])

    queue = follow_ups(_recommendations(client, (uri,)))
    if not queue and artist_name:
        try:
            queue = follow_ups([
                candidate for candidate in _search_tracks(client, f"artist:{artist_name}")
                if candidate and any(_name_key(name) == _name_key(artist_name) for name in _credits(candidate))
            ])
        except (SpotifyException, RequestException):
            pass
    album_uri = (track.get("album") or {}).get("uri")
    if not queue and _valid_uri(album_uri, "album"):
        try:
            album = client.album_tracks(album_uri, limit=30) or {}
            queue = follow_ups(album.get("items") or [])
        except (SpotifyException, RequestException):
            pass
    label = f"{title} by {artist_name}" if artist_name else title
    return _Playback(label, uris=(uri, *queue))


def play_track(title: str | None = None, artist: str | None = None) -> str:
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title and not artist:
        raise RuntimeError("Provide a track title or artist for Spotify playback.")

    def resolve(client: spotipy.Spotify) -> _Playback:
        if title and not artist and (linked := _linked_playlist(client, title)):
            return linked
        return _track_queue(client, _search_track(client, title, artist), artist)

    return _play(resolve)


def play_playlist(name: str) -> str:
    query = _name_key(name)
    if not query:
        raise RuntimeError("Provide a Spotify playlist name.")
    def resolve(client: spotipy.Spotify) -> _Playback:
        linked = _linked_playlist(client, name)
        if linked:
            return linked
        uri, label = _find_playlist(client, query)
        return _Playback(f"playlist {label}", context_uri=uri, shuffle=True)

    return _play(resolve, scope=_PLAYLIST_SCOPE)


def play_artist(name: str) -> str:
    name = name.strip()
    if not name:
        raise RuntimeError("Provide a Spotify artist name.")

    def resolve(client: spotipy.Spotify) -> _Playback:
        if linked := _linked_playlist(client, name):
            return linked
        result = client.search(q=f"artist:{name}", type="artist", limit=10) or {}
        for artist in (result.get("artists") or {}).get("items") or []:
            if (
                artist and _valid_uri(artist.get("uri"), "artist")
                and _name_key(artist.get("name") or "") == _name_key(name)
            ):
                return _Playback(artist["name"], context_uri=artist["uri"], shuffle=True)
        raise RuntimeError("Spotify couldn't find an artist with that exact name.")

    return _play(resolve)


def play_mix(query: str) -> str:
    query = " ".join(query.split())
    if not query:
        raise RuntimeError("Provide a mood or genre for the Spotify mix.")

    def resolve(client: spotipy.Spotify) -> _Playback:
        if linked := _linked_playlist(client, query):
            return linked
        result = client.search(q=query, type="playlist,track", limit=10) or {}
        playlists = (result.get("playlists") or {}).get("items") or []
        context_uri = next(
            (item["uri"] for item in playlists if item and _valid_uri(item.get("uri"), "playlist")), None,
        )
        tracks = (result.get("tracks") or {}).get("items") or []
        uris = _track_uris(tracks)
        if not context_uri:
            if len(uris) < 2:
                uris = _track_uris([*tracks, *_search_tracks(client, f"{query} mix")])
            if not uris:
                raise RuntimeError("Spotify couldn't find music for that mood or genre.")
            uris = tuple(dict.fromkeys((*uris, *_track_uris(_recommendations(client, uris)))))
        return _Playback(f"a {query} mix", uris=uris, context_uri=context_uri, shuffle=True)

    return _play(resolve)


def play_track_uri(uri: str) -> str:
    if not _valid_uri(uri, "track"):
        raise RuntimeError("Choose a valid Spotify track.")

    def resolve(client: spotipy.Spotify) -> _Playback:
        track = client.track(uri)
        if not _playable(track) or not track.get("name"):
            raise RuntimeError("Spotify couldn't find that playable track.")
        return _track_queue(client, track)

    return _play(resolve)


def account() -> dict:
    with _request("loading your account"):
        client = _client()
        profile = _profile(client)
        devices = (client.devices() or {}).get("devices") or []
        return {
            "name": profile.get("display_name") or profile["id"],
            "devices": [
                {"name": device.get("name") or "Unnamed device", "active": bool(device.get("is_active"))}
                for device in devices
            ],
        }


def top_tracks() -> list[dict]:
    with _request("loading your top tracks"):
        client = _client(scope=_TOP_TRACKS_SCOPE)
        result = client.current_user_top_tracks(limit=10, time_range="medium_term") or {}
        return [
            {"uri": track["uri"], "name": track["name"], "artist": ", ".join(_credits(track))}
            for track in result.get("items") or [] if _playable(track) and track.get("name")
        ]


def playlists() -> list[dict]:
    with _request("loading your playlists"):
        client = _client(scope=_PLAYLIST_SCOPE)
        linked = _playlist_links().get(_profile(client)["id"], {})
        library = _library_playlists(client)
        return [
            {"uri": uri, "name": name, "linkedName": linked.get(uri)}
            for uri, name in sorted((linked | library).items(), key=lambda entry: _name_key(entry[1]))
        ]


def set_playlist_link(uri: str, linked: bool) -> str:
    if not _valid_uri(uri, "playlist"):
        raise RuntimeError("Choose a valid Spotify playlist.")
    with _request("updating your linked playlists"):
        client = _client(scope=_PLAYLIST_SCOPE)
        account_id = _profile(client)["id"]
        name = _library_playlists(client).get(uri) if linked else None
        if linked and not name:
            raise RuntimeError("Choose a playlist from your Spotify library.")
        try:
            with _lock(_LINKS_PATH):
                accounts = _playlist_links()
                links = accounts.setdefault(account_id, {})
                if name:
                    if any(_name_key(other) == _name_key(name) for key, other in links.items() if key != uri):
                        raise RuntimeError("A linked playlist already has that name. Use unique names in Spotify.")
                    links[uri] = name
                else:
                    links.pop(uri, None)
                _write_private_json(_LINKS_PATH, accounts)
        except OSError:
            raise RuntimeError("Keni couldn’t save its linked Spotify playlists.") from None
        return f'Linked {name}. Say “Play {name}.”' if name else "Spotify playlist unlinked."
