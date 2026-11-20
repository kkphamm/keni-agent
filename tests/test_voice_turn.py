import asyncio
from datetime import UTC
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from livekit import rtc

from agent import actions, history, voice


class VoiceTurnTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_commits_each_microphone_turn_for_a_live_desktop_release(self):
        handlers = {}
        room = MagicMock(connection_state=rtc.ConnectionState.CONN_CONNECTED)
        room.on.side_effect = lambda event: lambda handler: handlers.setdefault(event, handler)
        desktop = SimpleNamespace(identity=voice.settings.DESKTOP_IDENTITY, track_publications={})
        room.remote_participants = {desktop.identity: desktop}
        ctx = SimpleNamespace(connect=AsyncMock(), room=room)
        session = MagicMock(start=AsyncMock())
        session.on.side_effect = lambda event: lambda handler: handlers.setdefault(event, handler)
        turn = asyncio.get_running_loop().create_future()
        turn.set_result("test turn")
        session.commit_user_turn.return_value = turn

        def microphone(sid):
            publication = SimpleNamespace(sid=sid, source=rtc.TrackSource.SOURCE_MICROPHONE)
            handlers["track_subscribed"](None, publication, desktop)

        def release(sender=desktop, data=b"release", kind=rtc.DataPacketKind.KIND_RELIABLE):
            handlers["data_received"](rtc.DataPacket(data, kind, sender, "keni.release"))

        async def start(**kwargs):
            microphone("first")
            release()

        session.start.side_effect = start
        with (
            patch.dict("os.environ", {"KENI_REALTIME_MODEL": ""}),
            patch.object(voice, "AgentSession", return_value=session) as agent_session,
            patch.object(voice.openai.realtime, "RealtimeModel") as realtime,
        ):
            await voice.entrypoint(ctx)
        self.assertEqual(agent_session.call_args.kwargs["turn_handling"], {"turn_detection": "manual"})
        options = realtime.call_args.kwargs
        self.assertEqual(options["model"], "gpt-realtime-2.1-mini")
        self.assertIsNone(options["turn_detection"])
        self.assertEqual(options["reasoning"].effort, "minimal")
        self.assertEqual(options["truncation"].token_limits.post_instructions, 4000)
        realtime.return_value.update_options.assert_called_once_with(max_response_output_tokens=512)

        release(sender=SimpleNamespace(identity="other-participant"))
        release(data=b"wrong payload")
        release(kind=rtc.DataPacketKind.KIND_LOSSY)
        session.commit_user_turn.assert_not_called()

        release()
        microphone("first")
        release()
        session.commit_user_turn.assert_called_once_with()

        microphone("second")
        room.connection_state = rtc.ConnectionState.CONN_DISCONNECTED
        release()
        room.connection_state = rtc.ConnectionState.CONN_CONNECTED
        handlers["close"](SimpleNamespace(error=None))
        release()
        session.commit_user_turn.assert_called_once_with()

    async def test_history_failure_keeps_the_real_action_outcome_and_start_time(self):
        room = MagicMock(remote_participants={})
        room.name = "keni-test"
        room.local_participant.publish_data = AsyncMock()
        ctx = SimpleNamespace(connect=AsyncMock(), room=room)
        session = MagicMock(start=AsyncMock())

        with (
            patch.object(voice, "AgentSession", return_value=session),
            patch.object(voice.openai.realtime, "RealtimeModel"),
            patch.object(history, "save_action", side_effect=[history.HistoryUnavailable(), None]) as save,
            patch.object(actions, "open_irdx", return_value="Opened IRDX in VS Code."),
        ):
            await voice.entrypoint(ctx)
            perform = session.start.await_args.kwargs["agent"].tools[0]
            with self.assertLogs(voice.logger, level="WARNING"):
                result = await perform(MagicMock(), open_irdx_in_vscode=True)

        self.assertEqual(result, {"vscode": "Opened IRDX in VS Code."})
        self.assertEqual(save.await_count, 2)
        running, final = [record.args for record in save.await_args_list]
        self.assertEqual(running[0], "keni-test")
        self.assertEqual(running[1]["id"], final[1]["id"])
        self.assertEqual(running[2], final[2])
        self.assertEqual(running[2].tzinfo, UTC)

        events = [json.loads(event.args[0]) for event in room.local_participant.publish_data.await_args_list]
        self.assertEqual([event["state"] for event in events], ["running", "succeeded"])
        self.assertEqual(events[-1]["message"], result["vscode"])
        self.assertIn("historyWarning", events[-1])
