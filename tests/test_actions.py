import asyncio
from pathlib import Path
import plistlib
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from agent import actions


def make_app(directory: Path, filename: str, display_name: str) -> Path:
    app = directory / filename
    contents = app / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleDisplayName": display_name,
        "CFBundleIdentifier": "com.example.installed-app",
        "CFBundlePackageType": "APPL",
    }))
    return app


class ActionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_artist_only_request_runs_the_spotify_action(self):
        async def publish(_event):
            pass

        with patch.object(actions.spotify_tools, "play_track", return_value="Playing Polyrhythm by Perfume.") as play:
            result = await actions.run_actions(publish, spotify_artist="Perfume")

        play.assert_called_once_with(None, "Perfume")
        self.assertEqual(result, {"spotify": "Playing Polyrhythm by Perfume."})

    async def test_playlist_runs_as_one_spotify_action(self):
        events = []

        async def publish(event):
            events.append(event)

        with (
            patch.object(actions.spotify_tools, "play_playlist", return_value="Playing playlist My Mix on this Mac.") as playlist,
            patch.object(actions.spotify_tools, "play_track") as track,
        ):
            result = await actions.run_actions(publish, spotify_playlist="My Mix", spotify_track="My Mix")

        playlist.assert_called_once_with("My Mix")
        track.assert_not_called()
        self.assertEqual(result, {"spotify": "Playing playlist My Mix on this Mac."})
        self.assertEqual([event["state"] for event in events], ["running", "succeeded"])

    async def test_spotify_mix_requests_route_to_the_named_playback_mode(self):
        async def publish(_event):
            pass

        for argument, operation, name in (
            ("spotify_artist_mix", "play_artist", "Perfume"),
            ("spotify_mix", "play_mix", "study lofi"),
        ):
            with (
                self.subTest(argument=argument),
                patch.object(actions.spotify_tools, operation, return_value="Playing a mix.") as play,
            ):
                self.assertEqual(await actions.run_actions(publish, **{argument: name}), {"spotify": "Playing a mix."})
                play.assert_called_once_with(name)

    async def test_independent_actions_overlap_and_report_their_own_outcome(self):
        events = []
        both_started = Barrier(2, timeout=2)

        async def publish(event):
            events.append(event)

        def play(_title, _artist):
            both_started.wait()
            raise RuntimeError("Spotify is not connected")

        def open_editor():
            both_started.wait()
            return "Opened IRDX in VS Code."

        with patch.object(actions.spotify_tools, "play_track", side_effect=play), patch.object(
            actions, "open_irdx", side_effect=open_editor
        ):
            result = await asyncio.wait_for(
                actions.run_actions(publish, spotify_track="Dynamite", open_irdx_in_vscode=True), timeout=4
            )
        self.assertEqual(result, {"spotify": "Spotify is not connected", "vscode": "Opened IRDX in VS Code."})
        for action, outcome in (("spotify", "failed"), ("vscode", "succeeded")):
            started, finished = [event for event in events if event["action"] == action]
            self.assertEqual((started["state"], finished["state"]), ("running", outcome))
            self.assertEqual(started["id"], finished["id"])

    def test_paths_are_configured_and_launched_as_literal_arguments(self):
        with TemporaryDirectory(prefix="Repository ") as directory:
            path = Path(directory).resolve()
            with patch.object(actions.settings, "irdx_path", return_value=path), patch.object(
                actions.subprocess, "run"
            ) as launch:
                with self.assertRaisesRegex(RuntimeError, "KENI_IRDX_PATH"):
                    actions.open_irdx()
                launch.assert_not_called()
                (path / ".git").touch()
                actions.open_irdx()
                self.assertEqual(launch.call_args.args[0], ["/usr/bin/open", "-b", "com.microsoft.VSCode", str(path)])

    def test_mac_app_aliases_use_approved_bundle_ids_and_keep_credentials_private(self):
        with (
            patch.dict("os.environ", {
                "KENI_MAC_APPS": '{"ChatGPT":"com.openai.codex"}',
                "OPENAI_API_KEY": "test-secret",
                "KENI_DATABASE_URL": "test-database",
                "HOME": "/Users/tester",
            }, clear=True),
            patch.object(actions.subprocess, "run") as launch,
        ):
            self.assertEqual(actions.open_app(" Chrome "), "Opened Google Chrome.")
            self.assertEqual(actions.open_app("chat gpt"), "Opened ChatGPT.")
            self.assertEqual([call.args[0] for call in launch.call_args_list], [
                ["/usr/bin/open", "-b", "com.google.Chrome"],
                ["/usr/bin/open", "-b", "com.openai.codex"],
            ])
            for call in launch.call_args_list:
                self.assertEqual(call.kwargs["env"], {"HOME": "/Users/tester"})

    def test_unapproved_names_and_invalid_configuration_cannot_launch_apps(self):
        with patch.object(actions, "APP_DIRECTORIES", ()), patch.object(actions.subprocess, "run") as launch:
            with patch.dict("os.environ", {"KENI_MAC_APPS": "{}"}):
                with self.assertRaisesRegex(RuntimeError, "No installed app matches"):
                    actions.open_app("/Applications/Google Chrome.app")
            with patch.dict("os.environ", {"KENI_MAC_APPS": '{"Chrome":"--args"}'}):
                with self.assertRaisesRegex(RuntimeError, "KENI_MAC_APPS"):
                    actions.open_app("Chrome")
            launch.assert_not_called()

    def test_discovers_installed_app_by_exact_filename_or_display_name(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            app = make_app(root, "Claude.app", "Claude Desktop")
            invalid = root / "Other.app" / "Contents"
            invalid.mkdir(parents=True)
            (invalid / "Info.plist").write_text("<?xml version='1.0'?><plist>")
            with (
                patch.dict("os.environ", {"KENI_MAC_APPS": "{}"}),
                patch.object(actions, "APP_DIRECTORIES", (root,)),
                patch.object(actions.subprocess, "run") as launch,
            ):
                self.assertEqual(actions.open_app(" claude   DESKTOP "), "Opened Claude.")
                self.assertEqual(actions.open_app("claude"), "Opened Claude.")
                self.assertEqual([call.args[0] for call in launch.call_args_list], [
                    ["/usr/bin/open", str(app)], ["/usr/bin/open", str(app)],
                ])
                with self.assertRaisesRegex(RuntimeError, "No installed app matches"):
                    actions.open_app("Other")

    def test_duplicate_installed_app_names_require_configured_target(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            make_app(root, "Claude One.app", "Claude")
            make_app(root, "Claude Two.app", "Claude")
            with (
                patch.object(actions, "APP_DIRECTORIES", (root,)),
                patch.object(actions.subprocess, "run") as launch,
            ):
                with patch.dict("os.environ", {"KENI_MAC_APPS": "{}"}):
                    with self.assertRaisesRegex(RuntimeError, "More than one installed app"):
                        actions.open_app("Claude")
                launch.assert_not_called()
                with patch.dict("os.environ", {"KENI_MAC_APPS": '{"Claude":"com.example.chosen"}'}):
                    self.assertEqual(actions.open_app("Claude"), "Opened Claude.")
                self.assertEqual(launch.call_args.args[0], ["/usr/bin/open", "-b", "com.example.chosen"])

    async def test_missing_mac_app_reports_a_failed_action(self):
        events = []

        async def publish(event):
            events.append(event)

        with (
            patch.dict("os.environ", {"KENI_MAC_APPS": "{}"}),
            patch.object(actions.subprocess, "run", side_effect=CalledProcessError(1, "/usr/bin/open")),
        ):
            result = await actions.run_actions(publish, app_to_open="Notes")
        self.assertEqual(result, {"open_app": "Couldn’t open Notes. Check that the Mac app is installed."})
        self.assertEqual([event["state"] for event in events], ["running", "failed"])
        self.assertEqual(events[0]["id"], events[1]["id"])
