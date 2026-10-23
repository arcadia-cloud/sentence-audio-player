"""Desktop launcher: starts the local backend and opens an Edge app window."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("LISTENING_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "data"
LOG = DATA / "logs"

EDGE_CANDIDATES = [
    os.environ.get("LISTENING_EDGE", ""),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

CREATE_NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    try:
        LOG.mkdir(parents=True, exist_ok=True)
        with open(LOG / "desktop.log", "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def healthy(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/api/config", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def find_edge() -> str | None:
    for p in EDGE_CANDIDATES:
        if p and Path(p).exists():
            return p
    return None


def wait_backend(proc: subprocess.Popen | None, seconds: float = 120) -> bool:
    start = time.time()
    while time.time() - start < seconds:
        if healthy():
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.4)
    return False


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_open(port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    log("=== start ===")
    LOG.mkdir(parents=True, exist_ok=True)
    (DATA / "uploads").mkdir(exist_ok=True)
    (DATA / "audio").mkdir(exist_ok=True)
    (DATA / "work").mkdir(exist_ok=True)

    backend = None
    log(f"healthy_before={healthy()}")
    if not healthy():
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "LISTENING_DATA": str(DATA),
            "LISTENING_FRONTEND": str(ROOT / "frontend"),
        }
        model = os.environ.get("LISTENING_MODEL")
        if not model:
            local = ROOT.parent / "listening-spike" / "out" / "models" / "small.en"
            if local.exists():
                env["LISTENING_MODEL"] = str(local)

        log_fh = open(LOG / "backend.log", "a", encoding="utf-8")
        backend = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=str(ROOT / "backend"),
            env=env,
            stdout=log_fh,
            stderr=log_fh,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        log(f"backend_pid={backend.pid}")

    ok = wait_backend(backend)
    log(f"backend_ready={ok}")
    if not ok:
        if backend:
            backend.kill()
        raise SystemExit("后端启动失败，请查看 data/logs/backend.log")

    edge = find_edge()
    log(f"edge={edge}")
    if not edge:
        raise SystemExit("没有找到 Microsoft Edge，请安装 WebView2/Edge 或设置 LISTENING_EDGE")

    profile = DATA / "edge-profile" / uuid.uuid4().hex[:8]
    profile.mkdir(parents=True, exist_ok=True)
    debug_port = free_port()
    edge_args = [
        f"--app={URL}",
        "--window-size=1320,860",
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={debug_port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    ps_list = ", ".join("'" + a.replace("'", "''") + "'" for a in edge_args)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         f"Start-Process -FilePath '{edge}' -ArgumentList {ps_list}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=0x00000008 if os.name == "nt" else 0,
    )
    log(f"edge_launched debug_port={debug_port}")

    # Keep this process alive so the backend and browser survive; use the browser's
    # debug port as a liveness signal and clean up when the window closes.
    alive = False
    for _ in range(40):
        if port_open(debug_port):
            alive = True
            break
        time.sleep(0.5)
    log(f"debug_alive={alive}")

    if alive:
        misses = 0
        while True:
            time.sleep(1.2)
            if port_open(debug_port, 0.8):
                misses = 0
            else:
                misses += 1
                if misses >= 3:
                    break
        if backend is not None:
            backend.terminate()
            try:
                backend.wait(timeout=5)
            except subprocess.TimeoutExpired:
                backend.kill()
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
