import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from threading import Lock
from time import time
from unittest.mock import MagicMock, call, patch
from urllib.parse import urlencode

from spotipy.exceptions import SpotifyException

from agent import spotify_tools


class SpotifyPlaybackTests(unittest.TestCase):
    def setUp(self):
        support = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(spotify_tools, "_CACHE_PATH", support / "spotify.json"))
        self.enterContext(patch.object(spotify_tools, "_LINKS_PATH", support / "spotify-playlists.json"))
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
        self.client.recommendations.return_value = {"tracks": []}
        self.enterContext(patch.object(spotify_tools, "_client", return_value=self.client))
        self.enterContext(patch.object(spotify_tools, "sleep"))
        self.run_command = self.enterContext(patch.object(
            spotify_tools.subprocess, "run", return_value=CompletedProcess([], 0, stdout="Test Mac\n")
        ))

    def test_opens_spotify_hidden_and_waits_for_playback_on_the_local_mac(self):
        self.client.devices.side_effect = [{"devices": [self.speaker]}, {"devices": [self.speaker, self.mac]}]
        playing = {"item": {"uri": "spotify:track:bts"}, "is_playing": True}
        self.client.current_playback.side_effect = [
            None,
            dict(playing, device={"id": "remote"}),
            dict(playing, device={"id": "mac"}),
        ]
        self.client.recommendations.return_value = {"tracks": [
            {"name": "Dynamite", "uri": "spotify:track:bts"},
            {"name": "Butter", "uri": "spotify:track:butter"},
            {"name": "Butter", "uri": "spotify:track:butter"},
            {"name": "Unavailable", "uri": "spotify:track:unavailable", "is_playable": False},
        ]}

        self.assertEqual(
            spotify_tools.play_track("Dynamite", "BTS"),
            "Playing Dynamite by BTS on this Mac. 1 more track queued.",
        )
        self.client.search.assert_called_once_with(q="track:Dynamite artist:BTS", type="track", limit=10)
        self.client.recommendations.assert_called_once_with(seed_tracks=["bts"], limit=30)
        self.run_command.assert_any_call(
            ["/usr/bin/open", "-g", "-j", "-b", "com.spotify.client"], check=True, capture_output=True, timeout=5,
        )
        self.assertEqual(self.client.devices.call_count, 2)
        self.client.assert_has_calls([
            call.shuffle(state=False, device_id="mac"),
            call.start_playback(device_id="mac", uris=["spotify:track:bts", "spotify:track:butter"]),
        ])
        self.assertEqual(self.client.current_playback.call_count, 3)

    def test_retries_a_stale_local_device_id(self):
        self.client.devices.side_effect = [{"devices": [self.mac]}, {"devices": [dict(self.mac, id="new-mac")]}]
        self.client.start_playback.side_effect = [SpotifyException(404, -1, "Device not found"), None]

        plan = spotify_tools._Playback("Dynamite", uris=("spotify:track:bts",))
        self.assertEqual(spotify_tools._start_local_playback(self.client, plan), ("new-mac", plan))
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

        self.assertEqual(
            spotify_tools.play_track(artist=" perfume "),
            "Playing Polyrhythm by Perfume on this Mac. No follow-up tracks were available.",
        )
        self.client.search.assert_any_call(q="artist:perfume", type="track", limit=10)
        self.client.start_playback.assert_called_once_with(device_id="mac", uris=["spotify:track:perfume"])

    def test_artist_only_does_not_launch_without_an_exact_match(self):
        with self.assertRaisesRegex(RuntimeError, "playable track by that exact artist"):
            spotify_tools.play_track(artist="Perfume")
        self.run_command.assert_not_called()
        self.client.start_playback.assert_not_called()

    def test_restricted_recommendations_fall_back_to_catalog_and_disable_single_track_repeat(self):
        self.client.recommendations.side_effect = SpotifyException(404, -1, "Unavailable")
        self.client.search.side_effect = [
            self.client.search.return_value,
            {"tracks": {"items": [
                {"name": "Dynamite", "uri": "spotify:track:alternate", "artists": [{"name": "BTS"}]},
                {"name": "Butter", "uri": "spotify:track:butter", "artists": [{"name": "BTS"}]},
                {"name": "Other", "uri": "spotify:track:other", "artists": [{"name": "Not BTS"}]},
            ]}},
        ]
        self.client.current_playback.side_effect = [
            {"item": {"uri": "spotify:track:butter"}, "device": {"id": "mac"}, "is_playing": True, "shuffle_state": True},
            {
                "item": {"uri": "spotify:track:bts"}, "device": {"id": "mac"}, "is_playing": True,
                "shuffle_state": False, "repeat_state": "track",
            },
        ]
        self.assertIn("1 more track queued", spotify_tools.play_track("Dynamite", "BTS"))
        self.client.search.assert_called_with(q="artist:BTS", type="track", limit=10)
        self.client.start_playback.assert_called_once_with(device_id="mac", uris=["spotify:track:bts", "spotify:track:butter"])
        self.client.repeat.assert_called_once_with(state="off", device_id="mac")
        self.assertEqual(self.client.current_playback.call_count, 2)

    def test_opens_a_new_player_before_setting_ordered_queue_if_shuffle_needs_an_active_track(self):
        self.client.shuffle.side_effect = [SpotifyException(404, -1, "No active device"), None]
        plan = spotify_tools._Playback("Dynamite", uris=("spotify:track:bts", "spotify:track:butter"))
        self.assertEqual(spotify_tools._start_local_playback(self.client, plan), ("mac", plan))
        self.client.start_playback.assert_has_calls([
            call(device_id="mac", uris=["spotify:track:bts"]),
            call(device_id="mac", uris=["spotify:track:bts", "spotify:track:butter"]),
        ])

    def test_artist_catalog_plays_as_a_shuffled_context(self):
        self.client.search.return_value = {"artists": {"items": [
            {"name": "Perfume Genius", "uri": "spotify:artist:other"},
            {"name": "Perfume", "uri": "spotify:artist:perfume"},
        ]}}
        self.client.current_playback.return_value = {
            "context": {"uri": "spotify:artist:perfume"}, "device": {"id": "mac"}, "is_playing": True,
            "shuffle_state": True,
        }
        self.assertEqual(spotify_tools.play_artist("Perfume"), "Playing Perfume on this Mac.")
        self.client.assert_has_calls([
            call.start_playback(device_id="mac", context_uri="spotify:artist:perfume"),
            call.shuffle(state=True, device_id="mac"),
        ])

    def test_mood_mix_falls_back_to_playable_tracks_if_playlist_context_is_denied(self):
        self.client.search.return_value = {
            "playlists": {"items": [None, {"uri": "spotify:playlist:lofihits"}]},
            "tracks": {"items": [{"uri": "spotify:track:one"}, {"uri": "spotify:track:two"}]},
        }
        self.client.start_playback.side_effect = [SpotifyException(403, -1, "Restricted"), None]
        self.client.current_playback.return_value = {
            "item": {"uri": "spotify:track:two"}, "device": {"id": "mac"}, "is_playing": True, "shuffle_state": True,
        }
        self.assertEqual(spotify_tools.play_mix("study lofi"), "Playing a study lofi mix on this Mac.")
        self.client.assert_has_calls([
            call.start_playback(device_id="mac", context_uri="spotify:playlist:lofihits"),
            call.start_playback(device_id="mac", uris=["spotify:track:one", "spotify:track:two"]),
            call.shuffle(state=True, device_id="mac"),
        ])
        self.client.playlist_items.assert_not_called()

    def test_linked_playlist_takes_priority_by_name_for_its_account_only(self):
        uri = "spotify:playlist:EveningJazz"
        self.client.current_user.return_value = {"id": "account-one"}
        self.client.current_user_playlists.return_value = {"items": [{"name": "Evening Jazz", "uri": uri}]}
        self.client.current_playback.return_value = {
            "context": {"uri": uri}, "device": {"id": "mac"}, "is_playing": True, "shuffle_state": True,
        }

        spotify_tools.set_playlist_link(uri, True)
        self.assertEqual(spotify_tools._LINKS_PATH.stat().st_mode & 0o777, 0o600)
        self.assertEqual(spotify_tools.playlists(), [{"uri": uri, "name": "Evening Jazz", "linkedName": "Evening Jazz"}])
        self.assertEqual(spotify_tools.play_track(" evening jazz "), "Playing playlist Evening Jazz on this Mac.")
        self.client.search.assert_not_called()

        self.client.current_user.return_value = {"id": "account-two"}
        self.assertIsNone(spotify_tools.playlists()[0]["linkedName"])
        with self.assertRaisesRegex(RuntimeError, "exact match"):
            spotify_tools.play_track("Evening Jazz")
        self.client.start_playback.assert_called_once_with(device_id="mac", context_uri=uri)

        self.client.current_user.return_value = {"id": "account-one"}
        spotify_tools.set_playlist_link(uri, False)
        self.assertIsNone(spotify_tools.playlists()[0]["linkedName"])

    def test_top_tracks_and_direct_play_use_spotify_uris(self):
        track = {"uri": "spotify:track:bts", "name": "Dynamite", "artists": [{"name": "BTS"}]}
        self.client.current_user_top_tracks.return_value = {"items": [track, {"uri": "spotify:episode:episode"}]}
        self.client.track.return_value = track
        self.client.current_playback.return_value = {
            "item": {"uri": "spotify:track:bts"}, "device": {"id": "mac"}, "is_playing": True,
        }
        self.assertEqual(spotify_tools.top_tracks(), [{"uri": "spotify:track:bts", "name": "Dynamite", "artist": "BTS"}])
        self.client.current_user_top_tracks.assert_called_once_with(limit=10, time_range="medium_term")
        with self.assertRaisesRegex(RuntimeError, "valid Spotify track"):
            spotify_tools.play_track_uri("https://example.invalid/track")
        self.assertIn("Playing Dynamite", spotify_tools.play_track_uri("spotify:track:bts"))
        self.client.track.assert_called_once_with("spotify:track:bts")
        self.client.start_playback.assert_called_once_with(device_id="mac", uris=["spotify:track:bts"])

    def test_playlist_finds_exact_name_across_pages_and_verifies_the_local_context(self):
        uri = "spotify:playlist:EveningJazz123"
        self.client.current_user_playlists.side_effect = [
            {"items": [{"name": "Evening Jazz Classics", "uri": "spotify:playlist:Other123"}], "next": "more"},
            {"items": [{"name": "Evening Jazz", "uri": uri}]},
        ]
        self.client.current_playback.side_effect = [
            {"context": {"uri": uri}, "device": {"id": "remote"}, "is_playing": True},
            {"context": None, "device": {"id": "mac"}, "is_playing": True},
            {"context": {"uri": uri}, "device": {"id": "mac"}, "is_playing": True},
        ]

        self.assertEqual(spotify_tools.play_playlist(" evening  JAZZ "), "Playing playlist Evening Jazz on this Mac.")
        self.assertEqual(self.client.current_user_playlists.call_args_list, [call(limit=50, offset=0), call(limit=50, offset=50)])
        self.client.search.assert_not_called()
        self.client.start_playback.assert_called_once_with(device_id="mac", context_uri=uri)
        self.assertEqual(self.client.current_playback.call_count, 3)

    def test_unique_partial_playlist_requires_verified_playing_context(self):
        uri = "spotify:playlist:EveningJazz123"
        self.client.current_user_playlists.return_value = {"items": [{"name": "Evening Jazz", "uri": uri}]}
        self.client.current_playback.return_value = {"context": {"uri": uri}, "device": {"id": "mac"}, "is_playing": False}

        with self.assertRaisesRegex(RuntimeError, "playback couldn't be confirmed"):
            spotify_tools.play_playlist("jazz")
        self.client.start_playback.assert_called_once_with(device_id="mac", context_uri=uri)

    def test_no_playback_for_missing_ambiguous_or_nonplaylist_results(self):
        for other_name, other_uri, query, message in (
            ("Morning Jazz", "spotify:playlist:Different123", "jazz", "Say the full playlist name"),
            ("Evening Jazz", "spotify:playlist:Different123", "evening jazz", "exact name"),
            ("Unrelated", "spotify:track:Invalid123", "unrelated", "couldn't find a matching playlist"),
        ):
            with self.subTest(query=query):
                self.client.current_user_playlists.return_value = {"items": [
                    {"name": "Evening Jazz", "uri": "spotify:playlist:EveningJazz123"},
                    {"name": other_name, "uri": other_uri},
                ]}
                with self.assertRaisesRegex(RuntimeError, message):
                    spotify_tools.play_playlist(query)
        self.run_command.assert_not_called()
        self.client.start_playback.assert_not_called()


class SpotifyAuthorizationTests(unittest.TestCase):
    def setUp(self):
        support = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(spotify_tools, "_CACHE_PATH", support / "spotify.json"))

    def test_playlist_scope_can_be_upgraded_without_breaking_old_track_access(self):
        token = {
            "scope": "user-read-playback-state user-modify-playback-state",
            "expires_at": int(time()) + 3600,
            "access_token": "old-token",
        }
        with (
            patch.dict("os.environ", {
                "SPOTIFY_CLIENT_ID": "app-id",
                "SPOTIFY_CLIENT_SECRET": "app-secret",
                "KENI_SPOTIFY_REDIRECT_URI": "http://127.0.0.1:8767/callback",
            }),
            patch.object(spotify_tools._TokenCache, "get_cached_token", return_value=token),
        ):
            self.assertIn("playlist-read-private", spotify_tools._oauth().scope.split())
            self.assertFalse(spotify_tools.connected())
            self.assertIsInstance(spotify_tools._client(), spotify_tools.spotipy.Spotify)
            with self.assertRaisesRegex(RuntimeError, "grant playlist access"):
                spotify_tools._client(scope=spotify_tools._PLAYLIST_SCOPE)
            token["scope"] += " playlist-read-private"
            self.assertTrue(spotify_tools.connected())
            self.assertIsInstance(spotify_tools._client(scope=spotify_tools._PLAYLIST_SCOPE), spotify_tools.spotipy.Spotify)
            self.assertFalse(spotify_tools.top_tracks_authorized())
            with self.assertRaisesRegex(RuntimeError, "Reauthorize"):
                spotify_tools.top_tracks()

            token.update(expires_at=int(time()) - 1, refresh_token="refresh-token")
            with patch.object(spotify_tools.SpotifyOAuth, "refresh_access_token", autospec=True, return_value=token) as refresh:
                self.assertIsInstance(spotify_tools._client(), spotify_tools.spotipy.Spotify)
            oauth, refresh_token = refresh.call_args.args
            self.assertEqual(refresh_token, "refresh-token")
            self.assertIn("playlist-read-private", oauth.scope.split())

    def test_sign_out_removes_only_the_local_oauth_cache(self):
        token_path = spotify_tools._CACHE_PATH
        token_path.write_text('{"access_token":"test-token"}')
        unrelated = token_path.with_name("spotify-playlists.json")
        unrelated.write_text("{}")
        self.assertIn("Signed out", spotify_tools.disconnect())
        self.assertFalse(token_path.exists())
        self.assertTrue(unrelated.exists())

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
