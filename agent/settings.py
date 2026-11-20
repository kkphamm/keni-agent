import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Local Voice Agent"
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 8765
WORKER_PORT = 8766
LIVEKIT_URL = "ws://127.0.0.1:7880"
DESKTOP_IDENTITY = "keni-desktop"
AGENT_NAME = "local-voice-agent"

load_dotenv(PROJECT_ROOT / ".env")


def irdx_path() -> Path:
    return Path(os.environ.get("KENI_IRDX_PATH", "~/Projects/irdx-R-D")).expanduser()
