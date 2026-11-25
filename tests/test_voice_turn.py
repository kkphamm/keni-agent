import asyncio
from datetime import UTC
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from livekit import rtc

from agent import actions, history, voice


class VoiceTurnTest(unittest.IsolatedAsyncioTestCase):
    async def test_quiet_mode_gates_only_agent_audio_and_accepts_the_desktop(self):
        handlers = {}
        room = MagicMock(remote_participants={})
        room.local_participant.register_rpc_method.side_effect = lambda name: (
            lambda handler: handlers.setdefault(name, handler)
        )
        ctx = SimpleNamespace(connect=AsyncMock(), room=room)
        session = MagicMock(start=AsyncMock())
        with (
            patch.object(voice, "AgentSession", return_value=session),
            patch.object(voice.openai.realtime, "RealtimeModel"),
        ):
            await voice.entrypoint(ctx)

        set_mode = handlers["keni.quiet_mode"]

        def request(payload, identity=voice.settings.DESKTOP_IDENTITY):
            return rtc.RpcInvocationData("request-id", identity, payload, 2.0)

        with self.assertRaises(rtc.RpcError):
            await set_mode(request("on", "other-participant"))
        with self.assertRaises(rtc.RpcError):
            await set_mode(request("toggle"))
        session.output.set_audio_enabled.assert_not_called()

        self.assertEqual(await set_mode(request("on")), "on")
        self.assertEqual(await set_mode(request("off")), "off")
        self.assertEqual([call.args for call in session.output.set_audio_enabled.call_args_list], [(False,), (True,)])
        session.output.set_transcription_enabled.assert_not_called()
        session.input.set_audio_enabled.assert_not_called()

    async def test_only_accepts_live_desktop_turn_controls(self):
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

        def send(sender=desktop, data=b"release", kind=rtc.DataPacketKind.KIND_RELIABLE, topic="keni.release"):
            handlers["data_received"](rtc.DataPacket(data, kind, sender, topic))

        async def start(**kwargs):
            microphone("first")
            send()
            send(data=b"interrupt", topic="keni.interrupt")

        session.start.side_effect = start
        with (
            patch.dict("os.environ", {"KENI_REALTIME_MODEL": ""}),
            patch.object(voice, "AgentSession", return_value=session) as agent_session,
            patch.object(voice.openai.realtime, "RealtimeModel") as realtime,
        ):
            await voice.entrypoint(ctx)
        self.assertEqual(agent_session.call_args.kwargs["turn_handling"], {"turn_detection": "manual"})
        self.assertEqual(agent_session.call_args.kwargs["aec_warmup_duration"], 0.0)
        options = realtime.call_args.kwargs
        self.assertEqual(options["model"], "gpt-realtime-2.1-mini")
        self.assertIsNone(options["turn_detection"])
        self.assertEqual(options["input_audio_transcription"].language, "en")
        self.assertEqual(options["input_audio_transcription"].model, "gpt-4o-mini-transcribe")
        self.assertEqual(options["reasoning"].effort, "minimal")
        self.assertEqual(options["truncation"].token_limits.post_instructions, 4000)
        realtime.return_value.update_options.assert_called_once_with(max_response_output_tokens=512)

        other = SimpleNamespace(identity="other-participant")
        lossy = rtc.DataPacketKind.KIND_LOSSY
        send(sender=other)
        send(data=b"wrong payload")
        send(kind=lossy)
        send(data=b"interrupt", sender=other, topic="keni.interrupt")
        send(data=b"interrupt", kind=lossy, topic="keni.interrupt")
        send(data=b"release", topic="keni.interrupt")
        session.interrupt.assert_not_called()
        send(data=b"interrupt", topic="keni.interrupt")
        session.interrupt.assert_called_once_with(force=True)
        session.commit_user_turn.assert_not_called()

        send()
        microphone("first")
        send()
        session.commit_user_turn.assert_called_once_with()

        microphone("second")
        send(data=b"release:1")
        send(data=b"release:1")
        send(data=b"release:0")
        send(data=b"release:18446744073709551616")
        send(data=b"release:invalid")
        send(sender=other, data=b"release:99")
        send(data=b"release:99", kind=lossy)
        send(data=b"release:2")
        send(data=b"release:1")
        send()  # An old-format packet cannot commit a prepared-track turn again.
        send(data=b"cancel:3")
        send(data=b"cancel:3")
        send(data=b"release:3")
        session.clear_user_turn.assert_called_once_with()
        self.assertEqual(session.commit_user_turn.call_count, 3)

        room.connection_state = rtc.ConnectionState.CONN_DISCONNECTED
        send(data=b"release:4")
        send(data=b"interrupt", topic="keni.interrupt")
        room.connection_state = rtc.ConnectionState.CONN_CONNECTED
        handlers["close"](SimpleNamespace(error=None))
        send(data=b"release:4")
        send(data=b"interrupt", topic="keni.interrupt")
        session.interrupt.assert_called_once_with(force=True)
        self.assertEqual(session.commit_user_turn.call_count, 3)

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
