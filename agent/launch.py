import fcntl
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

from agent import settings


def stop(_signal, _frame) -> None:
    raise SystemExit(0)


def wait_until_ready(url: str, process: subprocess.Popen, service: str) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with urlopen(url, timeout=0.5):
                return
        except OSError:
            time.sleep(0.15)
    raise RuntimeError(f"{service} could not start. See service.log for details.")


def serve(log) -> None:
    livekit = shutil.which("livekit-server", path=f"{os.defpath}:/opt/homebrew/bin:/usr/local/bin")
    if not livekit:
        raise RuntimeError("LiveKit Server is missing. Install it with brew install livekit.")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is missing. Add it to the project .env.")

    ports = [(port, socket.SOCK_STREAM) for port in (7880, settings.CONTROL_PORT, settings.WORKER_PORT)]
    ports.append((7882, socket.SOCK_DGRAM))
    for port, kind in ports:
        with socket.socket(type=kind) as probe:
            if kind == socket.SOCK_STREAM:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((settings.CONTROL_HOST, port))
            except OSError:
                raise RuntimeError(f"Loopback port {port} is already in use.") from None

    key = "keni"
    secret = secrets.token_hex(32)
    base_env = os.environ.copy()
    livekit_env = {
        **base_env,
        "LIVEKIT_KEYS": f"{key}: {secret}",
        "LIVEKIT_CONFIG": """
rtc:
  tcp_port: 0
  udp_port: 7882
  enable_loopback_candidate: true
  ips:
    includes: [127.0.0.1/32]
""",
    }
    for name in ("OPENAI_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "KENI_DATABASE_URL"):
        livekit_env.pop(name, None)
    worker_env = {**base_env, "LIVEKIT_URL": settings.LIVEKIT_URL,
                  "LIVEKIT_API_KEY": key, "LIVEKIT_API_SECRET": secret}

    processes = []

    def start(command: list[str], env: dict[str, str]) -> subprocess.Popen:
        process = subprocess.Popen(command, env=env, cwd=settings.PROJECT_ROOT,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append(process)
        return process

    desktop_token = settings.APP_SUPPORT / "desktop-token"
    desktop_token.write_text(secrets.token_urlsafe(32))
    desktop_token.chmod(0o600)
    try:
        transport = start([livekit, "--bind", settings.CONTROL_HOST, "--node-ip", settings.CONTROL_HOST], livekit_env)
        wait_until_ready("http://127.0.0.1:7880/", transport, "LiveKit")
        worker = start([sys.executable, "-m", "agent.voice", "start"], worker_env)
        wait_until_ready(f"http://127.0.0.1:{settings.WORKER_PORT}/", worker, "Voice worker")
        start([sys.executable, "-m", "uvicorn", "agent.desktop:app", "--host", settings.CONTROL_HOST,
               "--port", str(settings.CONTROL_PORT), "--no-access-log"], worker_env)
        while all(process.poll() is None for process in processes):
            time.sleep(0.25)
        raise RuntimeError("A local service stopped. See service.log for details.")
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        desktop_token.unlink(missing_ok=True)


def main() -> int:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    os.umask(0o077)
    settings.APP_SUPPORT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.APP_SUPPORT / "launch.lock").open("a") as lock:
        deadline = time.monotonic() + 12
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print("Another local voice service is already running.", file=sys.stderr)
                    return 1
                time.sleep(0.2)
        log_path = settings.APP_SUPPORT / "service.log"
        with log_path.open("w", buffering=1) as log:
            try:
                serve(log)
            except (OSError, RuntimeError) as exc:
                print(exc, file=log)
                print(exc, file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
