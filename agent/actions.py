import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import os
from pathlib import Path
import plistlib
import re
import subprocess
from uuid import uuid4
from xml.parsers.expat import ExpatError

from agent import settings, spotify_tools

logger = logging.getLogger(__name__)
Publish = Callable[[dict[str, str]], Awaitable[None]]

MAC_APPS = {
    "Google Chrome": "com.google.Chrome",
    "ChatGPT": "com.openai.chat",
    "Safari": "com.apple.Safari",
    "Finder": "com.apple.finder",
    "Notes": "com.apple.Notes",
    "Calendar": "com.apple.iCal",
    "Mail": "com.apple.mail",
    "Messages": "com.apple.MobileSMS",
    "System Settings": "com.apple.systempreferences",
    "Terminal": "com.apple.Terminal",
    "Spotify": "com.spotify.client",
    "VS Code": "com.microsoft.VSCode",
}
MAC_APP_ALIASES = {"chrome": "google chrome", "chat gpt": "chatgpt", "vscode": "vs code", "visual studio code": "vs code"}
APP_DIRECTORIES = (
    Path("/Applications"), Path("/Applications/Utilities"), Path.home() / "Applications",
    Path("/System/Applications"), Path("/System/Applications/Utilities"),
)
BUNDLE_ID = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def _open(*arguments: str) -> None:
    gui_environment = {
        key: os.environ[key]
        for key in ("HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    subprocess.run(
        ["/usr/bin/open", *arguments],
        check=True,
        capture_output=True,
        timeout=10,
        env=gui_environment,
    )


def _app_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _installed_app(key: str) -> Path:
    matched = None
    for directory in APP_DIRECTORIES:
        for bundle in directory.glob("*.app"):
            plist = bundle / "Contents" / "Info.plist"
            try:
                if bundle.is_symlink() or plist.parent.is_symlink() or plist.is_symlink() or not plist.is_file():
                    continue
                with plist.open("rb") as file:
                    info = plistlib.load(file)
            except (OSError, plistlib.InvalidFileException, ExpatError, ValueError, OverflowError):
                continue
            if not isinstance(info, dict) or info.get("CFBundlePackageType") != "APPL":
                continue
            bundle_id = info.get("CFBundleIdentifier")
            if not isinstance(bundle_id, str) or not BUNDLE_ID.fullmatch(bundle_id):
                continue
            names = (bundle.stem, info.get("CFBundleDisplayName"), info.get("CFBundleName"))
            if not any(isinstance(name, str) and _app_key(name) == key for name in names):
                continue
            if matched is not None:
                raise RuntimeError("More than one installed app matches that name. Configure the intended bundle ID in KENI_MAC_APPS.")
            matched = bundle
    if matched is None:
        raise RuntimeError("No installed app matches that name. Use its exact name or configure it in KENI_MAC_APPS.")
    return matched


def open_app(name: str) -> str:
    config_error = "KENI_MAC_APPS must be a JSON object mapping app names to macOS bundle IDs."
    try:
        configured = json.loads(os.getenv("KENI_MAC_APPS") or "{}")
    except json.JSONDecodeError:
        raise RuntimeError(config_error) from None
    if not isinstance(configured, dict) or any(
        not label.strip() or not isinstance(bundle_id, str) or not BUNDLE_ID.fullmatch(bundle_id)
        for label, bundle_id in configured.items()
    ):
        raise RuntimeError(config_error)

    apps = {_app_key(label): (label.strip(), bundle_id) for label, bundle_id in MAC_APPS.items()}
    apps.update({_app_key(label): (label.strip(), bundle_id) for label, bundle_id in configured.items()})
    key = _app_key(name)
    target = apps.get(key) or apps.get(MAC_APP_ALIASES.get(key, key))
    if target is None:
        bundle = _installed_app(key)
        label, arguments = bundle.stem, [str(bundle)]
    else:
        label, bundle_id = target
        arguments = ["-b", bundle_id]

    try:
        _open(*arguments)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(f"Couldn’t open {label}. Check that the Mac app is installed.") from None
    return f"Opened {label}."


def open_irdx() -> str:
    path = settings.irdx_path().resolve()
    if not path.is_dir() or not (path / ".git").exists():
        raise RuntimeError("The IRDX repository was not found. Check KENI_IRDX_PATH in .env.")
    try:
        _open("-b", "com.microsoft.VSCode", str(path))
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("macOS could not open IRDX in VS Code. Check that VS Code is installed.") from None
    return "Opened IRDX in VS Code."


async def run_actions(
    publish: Publish,
    *,
    spotify_track: str | None = None,
    spotify_artist: str | None = None,
    open_irdx_in_vscode: bool = False,
    app_to_open: str | None = None,
) -> dict[str, str]:
    async def run(action: str, operation: Callable[[], str]) -> tuple[str, str]:
        event = {"id": uuid4().hex, "action": action}

        async def report(state: str, message: str) -> None:
            try:
                await publish({**event, "state": state, "message": message})
            except Exception as exc:
                logger.warning("Could not deliver %s status (%s)", action, type(exc).__name__)

        await report("running", "Working…")
        try:
            message = await asyncio.to_thread(operation)
        except RuntimeError as exc:
            await report("failed", str(exc))
            return action, str(exc)
        except Exception as exc:
            logger.error("%s failed (%s)", action, type(exc).__name__)
            message = "The action failed unexpectedly. Please try again."
            await report("failed", message)
            return action, message
        await report("succeeded", message)
        return action, message

    jobs = []
    if (spotify_track or "").strip() or (spotify_artist or "").strip():
        jobs.append(run("spotify", lambda: spotify_tools.play_track(spotify_track, spotify_artist)))
    if open_irdx_in_vscode:
        jobs.append(run("vscode", open_irdx))
    if app_to_open and app_to_open.strip():
        jobs.append(run("open_app", lambda: open_app(app_to_open)))
    return dict(await asyncio.gather(*jobs))
