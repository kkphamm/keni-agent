import asyncio
from datetime import UTC, datetime
import json
import logging
import os

from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, cli, function_tool
from livekit.agents.voice import room_io
from livekit.plugins import openai
from openai.types.realtime.realtime_reasoning import RealtimeReasoning
from openai.types.realtime.realtime_truncation_retention_ratio import RealtimeTruncationRetentionRatio, TokenLimits

from agent import history, settings
from agent.actions import run_actions

logger = logging.getLogger(__name__)
server = AgentServer(host="127.0.0.1", port=settings.WORKER_PORT, num_idle_processes=1, drain_timeout=3)
DEFAULT_REALTIME_MODEL = "gpt-realtime-2.1-mini"


@server.rtc_session(agent_name=settings.AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    async def publish(topic: str, payload: dict[str, str]) -> None:
        try:
            await ctx.room.local_participant.publish_data(
                json.dumps(payload), topic=f"keni.{topic}", reliable=True,
                destination_identities=[settings.DESKTOP_IDENTITY],
            )
        except Exception as exc:
            logger.warning("Could not deliver %s event (%s)", topic, type(exc).__name__)

    @function_tool
    async def perform_actions(
        context: RunContext,
        spotify_track: str | None = None,
        spotify_artist: str | None = None,
        open_irdx_in_vscode: bool = False,
        app_to_open: str | None = None,
    ) -> dict[str, str]:
        """Perform the explicitly requested actions concurrently in a single call.

        Args:
            spotify_track: The requested Spotify track title. Omit it for an artist-only request.
            spotify_artist: The named artist. For "play a song by Perfume", pass Perfume with no track title.
            open_irdx_in_vscode: True only if the user asked to open IRDX in VS Code.
            app_to_open: One explicitly requested installed Mac app name, such as Claude, Chrome, or Notes.
        """
        context.disallow_interruptions()
        started_at: dict[str, datetime] = {}
        unsaved: set[str] = set()

        async def publish_action(event: dict[str, str]) -> None:
            action_id, state = event["id"], event["state"]
            start = started_at.setdefault(action_id, datetime.now(UTC))
            try:
                await history.save_action(ctx.room.name, event, start)
            except history.HistoryUnavailable:
                unsaved.add(action_id)
                logger.warning("Could not save %s history state %s", event["action"], state)

            if action_id in unsaved:
                event = {**event, "historyWarning": "Action history could not save every update."}
            if state != "running":
                started_at.pop(action_id, None)
                unsaved.discard(action_id)
            await publish("action", event)

        return await run_actions(
            publish_action,
            spotify_track=spotify_track, spotify_artist=spotify_artist,
            open_irdx_in_vscode=open_irdx_in_vscode, app_to_open=app_to_open,
        )

    model_name = os.getenv("KENI_REALTIME_MODEL") or DEFAULT_REALTIME_MODEL
    model = openai.realtime.RealtimeModel(
        model=model_name,
        turn_detection=None,
        reasoning=RealtimeReasoning(effort="minimal") if model_name == DEFAULT_REALTIME_MODEL else None,
        truncation=RealtimeTruncationRetentionRatio(
            type="retention_ratio", retention_ratio=0.8, token_limits=TokenLimits(post_instructions=4000),
        ),
    )
    model.update_options(max_response_output_tokens=512)
    session = AgentSession(
        llm=model,
        turn_handling={"turn_detection": "manual"},
        max_tool_steps=2,
    )
    ready = False
    pending_microphone_sid = None
    committed_microphone_sid = None

    @ctx.room.on("track_subscribed")
    def on_track_subscribed(
        _track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        nonlocal pending_microphone_sid
        if (
            participant.identity == settings.DESKTOP_IDENTITY
            and publication.source == rtc.TrackSource.SOURCE_MICROPHONE
            and publication.sid != committed_microphone_sid
        ):
            pending_microphone_sid = publication.sid

    def on_turn_committed(turn: asyncio.Future[str]) -> None:
        if not turn.cancelled() and (error := turn.exception()) is not None:
            logger.warning("Could not commit the microphone turn (%s)", type(error).__name__)

    @ctx.room.on("data_received")
    def on_release(packet: rtc.DataPacket) -> None:
        nonlocal pending_microphone_sid, committed_microphone_sid
        if (
            not ready
            or pending_microphone_sid is None
            or packet.topic != "keni.release"
            or packet.data != b"release"
            or packet.kind != rtc.DataPacketKind.KIND_RELIABLE
            or packet.participant is None
            or packet.participant.identity != settings.DESKTOP_IDENTITY
            or ctx.room.connection_state != rtc.ConnectionState.CONN_CONNECTED
        ):
            return

        committed_microphone_sid, pending_microphone_sid = pending_microphone_sid, None
        try:
            session.commit_user_turn().add_done_callback(on_turn_committed)
        except RuntimeError as exc:
            logger.warning("Could not commit the microphone turn (%s)", type(exc).__name__)

    async def report_failure(reason: str) -> None:
        await publish("session", {"message": "The voice connection failed. Check OpenAI access and try again."})
        ctx.shutdown(reason=reason)

    @session.on("close")
    def on_close(event) -> None:
        nonlocal ready
        ready = False
        if event.error is not None:
            asyncio.create_task(report_failure("Voice session failed"))

    try:
        await session.start(
            room=ctx.room,
            room_options=room_io.RoomOptions(participant_identity=settings.DESKTOP_IDENTITY),
            agent=Agent(
                instructions=(
                    "Speak one short sentence at a time. Only act on explicit requests to play a Spotify track "
                    "by title or choose a song by a named artist, open IRDX in VS Code, or open a named Mac app. Use the IRDX-specific "
                    "argument when asked for IRDX. Call perform_actions once with all requested actions, "
                    "including at most one named Mac app. Ask which app first if several are requested. "
                    "Ask for a track title or artist if neither was named. Use tool results to report each outcome. Never retry automatically."
                ),
                tools=[perform_actions],
            ),
        )
        if participant := ctx.room.remote_participants.get(settings.DESKTOP_IDENTITY):
            for publication in participant.track_publications.values():
                if publication.track is not None:
                    on_track_subscribed(publication.track, publication, participant)
        ready = True
    except Exception as exc:
        logger.error("Voice session startup failed (%s)", type(exc).__name__)
        await report_failure("Voice session startup failed")


if __name__ == "__main__":
    cli.run_app(server)
