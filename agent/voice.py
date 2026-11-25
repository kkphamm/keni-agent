import asyncio
from datetime import UTC, datetime
import json
import logging
import os

from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, cli, function_tool
from livekit.agents.voice import room_io
from livekit.plugins import openai
from openai.types.realtime.audio_transcription import AudioTranscription
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
        spotify_playlist: str | None = None,
        spotify_artist_mix: str | None = None,
        spotify_mix: str | None = None,
        open_irdx_in_vscode: bool = False,
        app_to_open: str | None = None,
    ) -> dict[str, str]:
        """Perform the explicitly requested actions concurrently in a single call.

        Args:
            spotify_track: The requested Spotify track title. For a linked playlist name with no stated artist, this can also resolve the playlist.
            spotify_artist: The artist credited on the requested track. For "play a song by Perfume", pass Perfume with no track title.
            spotify_playlist: The name of a requested playlist from the user's Spotify library. Omit track and artist for playlists.
            spotify_artist_mix: The artist name when asked for a mix of their music, e.g. "Play Perfume". Omit track, artist and playlist.
            spotify_mix: The requested genre or mood, e.g. "lo-fi for studying". Omit other Spotify arguments.
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
            spotify_track=spotify_track, spotify_artist=spotify_artist, spotify_playlist=spotify_playlist,
            spotify_artist_mix=spotify_artist_mix, spotify_mix=spotify_mix,
            open_irdx_in_vscode=open_irdx_in_vscode, app_to_open=app_to_open,
        )

    model_name = os.getenv("KENI_REALTIME_MODEL") or DEFAULT_REALTIME_MODEL
    model = openai.realtime.RealtimeModel(
        model=model_name,
        turn_detection=None,
        input_audio_transcription=AudioTranscription(
            model="gpt-4o-mini-transcribe",
            language="en",
            prompt="English macOS voice commands. Names include Keni, IRDX, VS Code, Spotify, ChatGPT, and Claude.",
        ),
        reasoning=RealtimeReasoning(effort="minimal") if model_name == DEFAULT_REALTIME_MODEL else None,
        truncation=RealtimeTruncationRetentionRatio(
            type="retention_ratio", retention_ratio=0.8, token_limits=TokenLimits(post_instructions=4000),
        ),
    )
    model.update_options(max_response_output_tokens=512)
    session = AgentSession(
        llm=model,
        turn_handling={"turn_detection": "manual"},
        # The desktop mutes playback on press, so keep the first input frames during an interruption.
        aec_warmup_duration=0.0,
        max_tool_steps=2,
    )

    @ctx.room.local_participant.register_rpc_method("keni.quiet_mode")
    async def set_quiet_mode(request: rtc.RpcInvocationData) -> str:
        if request.caller_identity != settings.DESKTOP_IDENTITY or request.payload not in ("on", "off"):
            raise rtc.RpcError(2001, "Invalid quiet mode request.")
        session.output.set_audio_enabled(request.payload == "off")
        return request.payload

    ready = False
    microphone_sid = None
    legacy_committed_sid = None
    last_release = 0

    @ctx.room.on("track_subscribed")
    def on_track_subscribed(
        _track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        nonlocal microphone_sid
        if (
            participant.identity == settings.DESKTOP_IDENTITY
            and publication.source == rtc.TrackSource.SOURCE_MICROPHONE
        ):
            microphone_sid = publication.sid

    def on_turn_committed(turn: asyncio.Future[str]) -> None:
        if not turn.cancelled() and (error := turn.exception()) is not None:
            logger.warning("Could not commit the microphone turn (%s)", type(error).__name__)

    @ctx.room.on("data_received")
    def on_turn_control(packet: rtc.DataPacket) -> None:
        nonlocal legacy_committed_sid, last_release
        if (
            not ready
            or packet.topic not in ("keni.interrupt", "keni.release")
            or packet.kind != rtc.DataPacketKind.KIND_RELIABLE
            or packet.participant is None
            or packet.participant.identity != settings.DESKTOP_IDENTITY
            or ctx.room.connection_state != rtc.ConnectionState.CONN_CONNECTED
        ):
            return

        if packet.topic == "keni.interrupt":
            if packet.data == b"interrupt":
                try:
                    session.interrupt(force=True)
                except RuntimeError as exc:
                    logger.warning("Could not stop agent speech (%s)", type(exc).__name__)
            return

        if microphone_sid is None:
            return

        prefix, _, sequence = packet.data.partition(b":")
        if packet.data == b"release":
            # Older desktops publish a fresh track for each turn.
            if last_release or microphone_sid == legacy_committed_sid:
                return
            legacy_committed_sid = microphone_sid
        else:
            if prefix not in (b"release", b"cancel") or len(sequence) > 20 or not sequence.isdigit():
                return
            turn_id = int(sequence)
            if not last_release < turn_id <= 2**64 - 1:
                return
            last_release = turn_id
        try:
            if prefix == b"cancel":
                session.clear_user_turn()
            else:
                session.commit_user_turn().add_done_callback(on_turn_committed)
        except RuntimeError as exc:
            logger.warning("Could not finish the microphone turn (%s)", type(exc).__name__)

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
            room_options=room_io.RoomOptions(
                participant_identity=settings.DESKTOP_IDENTITY,
                audio_input=room_io.AudioInputOptions(frame_size_ms=20),
            ),
            agent=Agent(
                instructions=(
                    "Expect spoken or typed English and always reply in English. Preserve app, artist, track, and playlist names. "
                    "Speak one short sentence at a time. Only act on explicit requests to play Spotify music, "
                    "open IRDX in VS Code, or open a named Mac app. Use at most one Spotify mode. "
                    "Use spotify_track with spotify_artist for a named song, spotify_artist alone for 'a song by' an artist, "
                    "spotify_artist_mix for 'play [artist]', spotify_mix for a mood or genre, and spotify_playlist for "
                    "'play my … playlist'. Pass an unclassified title such as 'My Chill Mix' as spotify_track without an artist "
                    "so the tool can find a linked playlist. Use the IRDX-specific "
                    "argument when asked for IRDX. Call perform_actions once with all requested actions, "
                    "including at most one named Mac app. Ask which app first if several are requested. "
                    "Ask the user to repeat any unclear request or name. Ask for a playlist name, track title, or artist when needed. "
                    "Use tool results to report each outcome. Never retry automatically."
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
