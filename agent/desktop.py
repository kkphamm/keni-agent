from datetime import timedelta
import secrets
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from livekit import api

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


@app.post("/spotify/authorize", dependencies=[Depends(verify_desktop)])
def spotify_authorize() -> dict[str, str]:
    try:
        return {"message": spotify_tools.authorize()}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None


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
