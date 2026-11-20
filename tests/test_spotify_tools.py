import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from subprocess import CompletedProcess
from threading import Lock
from unittest.mock import MagicMock, call, patch
from urllib.parse import urlencode

from spotipy.exceptions import SpotifyException

from agent import spotify_tools


class SpotifyPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.search.return_value = {
            "tracks": {"items": [
                {"name": "Dynamite", "artists": [{"name": "Taio Cruz"}], "uri": "spotify:track:other"},
                {"name": "Dynamite", "artists": [{"name": "BTS"}], "uri": "spotify:track:bts"},
            ]}
        }
        self.speaker = {"id": "remote", "type": "Speaker", "name": "Kitchen speaker", "is_active": True}
        self.mac = {"id": "mac", "type": "Computer", "name": "Test Mac", "is_active": False}
        self.client.devices.return_value = {"devices": [self.speaker, self.mac]}
        self.enterContext(patch.object(spotify_tools, "_client", return_value=self.client))
        self.enterContext(patch.object(spotify_tools, "sleep"))
        self.run_command = self.enterContext(patch.object(
            spotify_tools.subprocess, "run", return_value=CompletedProcess([], 0, stdout="Test Mac\n")
        ))

    def test_opens_spotify_and_waits_for_playback_on_the_local_mac(self):
        self.client.devices.side_effect = [{"devices": [self.speaker]}, {"devices": [self.speaker, self.mac]}]
        playing = {"item": {"uri": "spotify:track:bts"}, "is_playing": True}
        self.client.current_playback.side_effect = [
            None,
            dict(playing, device={"id": "remote"}),
            dict(playing, device={"id": "mac"}),
        ]

        self.assertEqual(spotify_tools.play_track("Dynamite", "BTS"), "Playing Dynamite by BTS on this Mac.")
        self.client.search.assert_called_once_with(q="track:Dynamite artist:BTS", type="track", limit=10)
        self.run_command.assert_any_call(
            ["/usr/bin/open", "-g", "-b", "com.spotify.client"], check=True, capture_output=True, timeout=5,
        )
        self.assertEqual(self.client.devices.call_count, 2)
        self.client.start_playback.assert_called_once_with(device_id="mac", uris=["spotify:track:bts"])
        self.assertEqual(self.client.current_playback.call_count, 3)

    def test_retries_a_stale_local_device_id(self):
        self.client.devices.side_effect = [{"devices": [self.mac]}, {"devices": [dict(self.mac, id="new-mac")]}]
        self.client.start_playback.side_effect = [SpotifyException(404, -1, "Device not found"), None]

        self.assertEqual(spotify_tools._start_local_playback(self.client, "spotify:track:bts"), "new-mac")
        self.assertEqual(self.client.start_playback.call_args_list, [
            call(device_id="mac", uris=["spotify:track:bts"]),
            call(device_id="new-mac", uris=["spotify:track:bts"]),
        ])

    def test_does_not_play_on_a_remote_device_when_the_mac_is_missing(self):
        self.client.devices.return_value = {"devices": [self.speaker]}
        with patch.object(spotify_tools, "monotonic", side_effect=[0, 0, 12]):
            with self.assertRaisesRegex(RuntimeError, "same Spotify account"):
                spotify_tools.play_track("Dynamite", "BTS")
        self.client.start_playback.assert_not_called()

    def test_playback_acknowledgment_alone_does_not_report_success(self):
        self.client.current_playback.return_value = {
            "item": {"uri": "spotify:track:bts"}, "device": {"id": "mac"}, "is_playing": False,
        }

        with self.assertRaisesRegex(RuntimeError, "playback couldn't be confirmed"):
            spotify_tools.play_track("Dynamite", "BTS")
        self.assertEqual(self.client.current_playback.call_count, 4)

    def test_missing_exact_artist_does_not_start_playback(self):
        with self.assertRaisesRegex(RuntimeError, "exact match"):
            spotify_tools.play_track("Dynamite", "Bruno Mars")
        self.run_command.assert_not_called()
        self.client.start_playback.assert_not_called()

    def test_shared_track_title_requires_an_artist_before_playback(self):
        with self.assertRaisesRegex(RuntimeError, "track title and artist"):
            spotify_tools.play_track("Dynamite")
        self.run_command.assert_not_called()
        self.client.start_playback.assert_not_called()

    def test_artist_only_plays_one_playable_exact_artist_match(self):
        self.client.search.return_value = {"tracks": {"items": [
            {"name": "Queen", "artists": [{"name": "Perfume Genius"}], "uri": "spotify:track:unrelated"},
            {
                "name": "Dream Fighter", "artists": [{"name": "Perfume"}],
                "uri": "spotify:track:unavailable", "is_playable": False,
            },
            {"name": "Polyrhythm", "artists": [{"name": "Perfume"}], "uri": "spotify:track:perfume"},
        ]}}
        self.client.current_playback.return_value = {
            "item": {"uri": "spotify:track:perfume"}, "device": {"id": "mac"}, "is_playing": True,
        }

        self.assertEqual(spotify_tools.play_track(artist=" perfume "), "Playing Polyrhythm by Perfume on this Mac.")
        self.client.search.assert_called_once_with(q="artist:perfume", type="track", limit=10)
        self.client.start_playback.assert_called_once_with(device_id="mac", uris=["spotify:track:perfume"])

    def test_artist_only_does_not_launch_without_an_exact_match(self):
        with self.assertRaisesRegex(RuntimeError, "playable track by that exact artist"):
            spotify_tools.play_track(artist="Perfume")
        self.run_command.assert_not_called()
        self.client.start_playback.assert_not_called()


class SpotifyAuthorizationTests(unittest.TestCase):
    def test_reports_occupied_callback_port_before_opening_browser(self):
        with HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as occupied:
            oauth = MagicMock(redirect_uri=f"http://127.0.0.1:{occupied.server_port}/callback")
            with (
                patch.object(spotify_tools, "_oauth", return_value=oauth),
                patch.object(spotify_tools.subprocess, "run") as open_browser,
            ):
                with self.assertRaisesRegex(RuntimeError, "KENI_SPOTIFY_REDIRECT_URI"):
                    spotify_tools.authorize()
                open_browser.assert_not_called()

    def test_only_matching_callback_exchanges_authorization_code(self):
        oauth = MagicMock(redirect_uri="http://127.0.0.1:8000/callback")
        oauth.get_authorize_url.return_value = "https://accounts.spotify.com/authorize"
        authorization = Lock()
        server = None

        def bind_to_random_port(_address, handler):
            nonlocal server
            server = HTTPServer(("127.0.0.1", 0), handler)
            return server

        with (
            patch.object(spotify_tools, "_oauth", return_value=oauth),
            patch.object(spotify_tools.subprocess, "run"),
            patch.object(spotify_tools, "_AUTHORIZATION_TIMEOUT", 3),
            patch.object(spotify_tools, "HTTPServer", side_effect=bind_to_random_port),
            patch.object(spotify_tools, "_authorization", authorization),
            patch.object(spotify_tools, "authorization_error", None),
        ):
            spotify_tools.authorize()
            state = oauth.get_authorize_url.call_args.kwargs["state"]
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            self.addCleanup(conn.close)

            def callback(state):
                query = urlencode({"state": state, "code": "test-code"})
                conn.request("GET", f"/callback?{query}")
                response = conn.getresponse()
                response.read()
                return response.status

            self.assertEqual(callback("wrong-state"), 400)
            oauth.get_access_token.assert_not_called()
            self.assertEqual(callback(state), 200)
            oauth.get_access_token.assert_called_once_with("test-code", as_dict=False, check_cache=False)
            self.assertTrue(authorization.acquire(timeout=4))
            authorization.release()

            spotify_tools.authorize()
            state = oauth.get_authorize_url.call_args.kwargs["state"]
            retry = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            self.addCleanup(retry.close)
            retry.request("GET", "/callback?" + urlencode({"state": state, "error": "server_error"}))
            response = retry.getresponse()
            self.assertEqual(response.status, 400)
            self.assertIn(b"sign-in error", response.read())
            self.assertIn("registered callback", spotify_tools.authorization_error)
            oauth.get_access_token.assert_called_once()
            self.assertTrue(authorization.acquire(timeout=4))


if __name__ == "__main__":
    unittest.main()
