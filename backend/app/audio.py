"""Audio helpers: ffmpeg extraction, duration probing, waveform peaks."""
from __future__ import annotations

import array
import json
import subprocess
import wave
from pathlib import Path


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg/ffprobe failed: {' '.join(cmd)}\n{p.stderr[-800:]}")
    return p.stdout


def probe_duration(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float(out.strip().splitlines()[0])
    except Exception:
        return 0.0


def extract_wav(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn",
          "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)])


def cut_wav(src: Path, dst: Path, start: float, duration: float) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, start):.3f}",
          "-t", f"{max(0.2, duration):.3f}", "-i", str(src),
          "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)])


def compute_peaks(wav_path: Path, buckets: int = 1600) -> list[float]:
    with wave.open(str(wav_path), "rb") as w:
        total = w.getnframes()
        per = max(1, total // buckets)
        peaks: list[float] = []
        while len(peaks) < buckets:
            raw = w.readframes(per)
            if not raw:
                break
            arr = array.array("h")
            arr.frombytes(raw[: len(raw) // 2 * 2])
            if arr:
                peak = max(max(arr), -min(arr)) / 32768.0
            else:
                peak = 0.0
            peaks.append(round(min(1.0, peak), 4))
    while len(peaks) < buckets:
        peaks.append(0.0)
    return peaks


def peaks_to_json(peaks: list[float]) -> str:
    return json.dumps(peaks, separators=(",", ":"))
