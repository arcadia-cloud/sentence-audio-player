"""Portable launcher for Listening Studio (PyInstaller entry point)."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from pathlib import Path

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

FROZEN = getattr(sys, "frozen", False)
RES = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent.parent
APP = Path(sys.executable).parent if FROZEN else RES

EDGE_CANDIDATES = [
    os.environ.get("LISTENING_EDGE", ""),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    p = Path(base) / "ListeningStudio"
    p.mkdir(parents=True, exist_ok=True)
    return p


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


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/config", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def find_edge() -> str | None:
    for p in EDGE_CANDIDATES:
        if p and Path(p).exists():
            return p
    return None


def log(path: Path, msg: str) -> None:
    try:
        with open(path / "launcher.log", "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def main() -> int:
    data = data_dir()
    log(data, "launcher start frozen=%s res=%s" % (FROZEN, RES))
    try:
        backend_log = open(data / "backend.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = backend_log
        sys.stderr = backend_log
    except Exception:
        pass

    os.environ["LISTENING_DATA"] = str(data)
    os.environ["LISTENING_CONFIG"] = str(data / "config.json")
    frontend = RES / "frontend"
    if frontend.exists():
        os.environ["LISTENING_FRONTEND"] = str(frontend)

    model = RES / "models" / "small.en"
    if model.exists():
        os.environ.setdefault("LISTENING_MODEL", str(model))

    bin_dir = RES / "bin"
    if bin_dir.exists():
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")

    cuda_dir = RES / "cuda"
    if cuda_dir.exists():
        os.environ["LISTENING_CUDA_DIR"] = str(cuda_dir)
        os.environ["PATH"] = str(cuda_dir) + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(str(cuda_dir))
        except Exception:
            pass

    backend = RES / "backend"
    if backend.exists():
        sys.path.insert(0, str(backend))
    try:
        import uvicorn

        from app.main import app
    except Exception as exc:
        log(data, f"import failed: {exc!r}")
        raise

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    threading.Thread(target=server.run, daemon=True).start()

    for _ in range(120):
        if healthy(url):
            break
        time.sleep(0.5)
    else:
        log(data, "backend did not become healthy")
        return 1

    log(data, f"backend ready {url}")
    if os.environ.get("LISTENING_NO_EDGE"):
        log(data, "no-edge mode; keeping backend alive")
        while True:
            time.sleep(3600)

    edge = find_edge()
    if not edge:
        log(data, "edge not found, opening default browser")
        webbrowser.open(url)
        while True:
            time.sleep(3600)

    profile = data / "edge-profile" / uuid.uuid4().hex[:8]
    profile.mkdir(parents=True, exist_ok=True)
    debug_port = free_port()
    subprocess.Popen([
        edge,
        f"--app={url}",
        "--window-size=1320,860",
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={debug_port}",
        "--no-first-run",
        "--no-default-browser-check",
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(data, f"edge launched debug_port={debug_port}")

    alive = False
    for _ in range(60):
        if port_open(debug_port):
            alive = True
            break
        time.sleep(0.5)

    if alive:
        started = time.time()
        while port_open(debug_port):
            time.sleep(1.0)
        elapsed = time.time() - started
        shutil.rmtree(profile, ignore_errors=True)
        log(data, f"edge closed after {elapsed:.1f}s")
        if elapsed < 10:
            log(data, "edge closed too early, opening default browser")
            webbrowser.open(url)
            while True:
                time.sleep(3600)
        server.should_exit = True
        time.sleep(0.5)
        return 0

    log(data, "edge debug port not detected; opening default browser")
    webbrowser.open(url)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    if FROZEN:
        import multiprocessing
        multiprocessing.freeze_support()
    raise SystemExit(main())
