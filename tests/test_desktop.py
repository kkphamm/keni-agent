from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from livekit import api

from agent import desktop


class DesktopTest(unittest.TestCase):
    def test_spotify_library_routes_are_private_and_validate_track_uris(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(desktop.settings, "APP_SUPPORT", Path(directory)),
            patch.object(desktop.spotify_tools, "top_tracks", return_value=[]) as top_tracks,
            patch.object(desktop.spotify_tools, "play_track_uri", return_value="Playing a track.") as play,
            TestClient(desktop.app) as client,
        ):
            (Path(directory) / "desktop-token").write_text("test-desktop-key\n")
            headers = {"Authorization": "Bearer test-desktop-key"}
            self.assertEqual(client.get("/spotify/top-tracks").status_code, 401)
            self.assertEqual(client.post("/spotify/play-track", json={"uri": "spotify:track:demo"}).status_code, 401)
            top_tracks.assert_not_called()
            play.assert_not_called()

            response = client.get("/spotify/top-tracks", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json(), {"tracks": []})
            response = client.post("/spotify/play-track", headers=headers, json={"uri": "spotify:playlist:demo"})
            self.assertEqual(response.status_code, 422)
            play.assert_not_called()
            response = client.post("/spotify/play-track", headers=headers, json={"uri": "spotify:track:demo"})
            self.assertEqual(response.json(), {"message": "Playing a track."})
            play.assert_called_once_with("spotify:track:demo")

    def test_session_requires_local_bearer_and_limits_join_token(self):
        key, secret = "test-key", "a-local-signing-secret-longer-than-32-chars"
        with TemporaryDirectory() as directory:
            support = Path(directory)
            (support / "desktop-token").write_text("desktop-test-key\n")
            with patch.object(desktop.settings, "APP_SUPPORT", support):
                with self.assertRaises(HTTPException) as denied:
                    desktop.verify_desktop("Bearer wrong")
                self.assertEqual(denied.exception.status_code, 401)
                desktop.verify_desktop("Bearer desktop-test-key")

        with patch.dict("os.environ", {"LIVEKIT_API_KEY": key, "LIVEKIT_API_SECRET": secret}):
            credentials = desktop.session()
            second = desktop.session()
        verifier = api.TokenVerifier(key, secret, leeway=timedelta(seconds=0))
        claims = verifier.verify(credentials["token"])
        self.assertEqual(claims.identity, desktop.settings.DESKTOP_IDENTITY)
        self.assertEqual(claims.video.can_publish_sources, ["microphone"])
        self.assertNotEqual(claims.video.room, verifier.verify(second["token"]).video.room)
