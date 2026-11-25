import secrets
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Response
from livekit import api
from pydantic import BaseModel, StringConstraints

from agent import gmail_tools, history, settings, spotify_tools

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def verify_desktop(authorization: Annotated[str, Header()] = "") -> None:
    try:
        token = (settings.APP_SUPPORT / "desktop-token").read_text().strip()
    except OSError:
        raise HTTPException(503, "The local desktop service is still starting.") from None
    if not token or not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "The app could not authenticate to its local service.")


@app.get("/health")
def health() -> dict[str, bool | str | None]:
    spotify_configured = spotify_tools.configured()
    gmail_configured = gmail_tools.configured()
    return {
        "spotifyConfigured": spotify_configured,
        "spotifyConnected": spotify_configured and spotify_tools.connected(),
        "spotifyTopTracksAuthorized": spotify_tools.top_tracks_authorized(),
        "spotifyAuthorizationPending": spotify_tools.authorization_pending(),
        "spotifyAuthorizationError": spotify_tools.authorization_error,
        "gmailConfigured": gmail_configured,
        "gmailConnected": gmail_configured and gmail_tools.connected(),
        "gmailAuthorizationError": gmail_tools.authorization_error,
    }


@app.get("/history", dependencies=[Depends(verify_desktop)])
async def action_history(response: Response) -> dict[str, list[dict[str, Any]]]:
    try:
        actions = await history.list_actions()
    except history.HistoryUnavailable:
        raise HTTPException(503, "Action history is unavailable.") from None
    response.headers["Cache-Control"] = "no-store"
    return {"actions": actions}


@app.post("/session", dependencies=[Depends(verify_desktop)])
def session() -> dict[str, str]:
    room = f"keni-{secrets.token_hex(16)}"
    token = (
        api.AccessToken()
        .with_identity(settings.DESKTOP_IDENTITY)
        .with_ttl(timedelta(minutes=5))
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish_sources=["microphone"]))
        .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=settings.AGENT_NAME)]))
        .to_jwt()
    )
    return {"url": settings.LIVEKIT_URL, "token": token}


spotify = APIRouter(prefix="/spotify", dependencies=[Depends(verify_desktop)])


def _spotify_result[T](response: Response, operation: Callable[[], T]) -> T:
    try:
        result = operation()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc), headers={"Cache-Control": "no-store"}) from None
    response.headers["Cache-Control"] = "no-store"
    return result


@spotify.post("/authorize")
def spotify_authorize(response: Response) -> dict[str, str]:
    return {"message": _spotify_result(response, spotify_tools.authorize)}


@spotify.post("/disconnect")
def spotify_disconnect(response: Response) -> dict[str, str]:
    return {"message": _spotify_result(response, spotify_tools.disconnect)}


@spotify.get("/account")
def spotify_account(response: Response) -> dict:
    return _spotify_result(response, spotify_tools.account)


@spotify.get("/top-tracks")
def spotify_top_tracks(response: Response) -> dict:
    return {"tracks": _spotify_result(response, spotify_tools.top_tracks)}


@spotify.get("/playlists")
def spotify_playlists(response: Response) -> dict:
    return {"playlists": _spotify_result(response, spotify_tools.playlists)}


class SpotifyTrack(BaseModel):
    uri: Annotated[str, StringConstraints(pattern=r"^spotify:track:[A-Za-z0-9]+$", max_length=100)]


class SpotifyPlaylistLink(BaseModel):
    uri: Annotated[str, StringConstraints(pattern=r"^spotify:playlist:[A-Za-z0-9]+$", max_length=100)]
    linked: bool


@spotify.post("/play-track")
def spotify_play_track(track: SpotifyTrack, response: Response) -> dict[str, str]:
    return {"message": _spotify_result(response, lambda: spotify_tools.play_track_uri(track.uri))}


@spotify.post("/playlist-link")
def spotify_playlist_link(link: SpotifyPlaylistLink, response: Response) -> dict[str, str]:
    return {"message": _spotify_result(response, lambda: spotify_tools.set_playlist_link(link.uri, link.linked))}


app.include_router(spotify)


@app.post("/gmail/authorize", dependencies=[Depends(verify_desktop)])
def gmail_authorize(response: Response) -> dict[str, str]:
    try:
        message = gmail_tools.authorize()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc), headers={"Cache-Control": "no-store"}) from None
    response.headers["Cache-Control"] = "no-store"
    return {"message": message}


@app.get("/gmail/inbox", dependencies=[Depends(verify_desktop)])
def gmail_inbox(response: Response, q: str = "") -> dict[str, list[dict[str, str]]]:
    if len(q) > 256:
        raise HTTPException(422, "Gmail search must be 256 characters or fewer.", headers={"Cache-Control": "no-store"})
    try:
        messages = gmail_tools.inbox(q)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc), headers={"Cache-Control": "no-store"}) from None
    response.headers["Cache-Control"] = "no-store"
    return {"messages": messages}
