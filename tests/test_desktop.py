from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from livekit import api

from agent import desktop


class DesktopTest(unittest.TestCase):
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
