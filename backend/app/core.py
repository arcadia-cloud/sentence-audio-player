#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Listening spike pipeline:
  audio -> faster-whisper word timestamps -> DP sentence segmentation -> self-contained HTML report

Usage:
  python pipeline.py all --audio "C:\\path\\file.mp3" --out out --model small.en
  python pipeline.py seg --json out/transcript.json --out out
  python pipeline.py report --json out/transcript.json --out out
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# HuggingFace mirror for mainland network; harmless elsewhere.
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------------------
@dataclass
class Word:
    text: str
    start: float
    end: float
    prob: float
    seg: int = 0


@dataclass
class Sentence:
    idx: int
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    avg_prob: float = 1.0
    terminal: bool = False
    pause_after: float = 0.0
    whisper_break: bool = False
    flags: list[str] = field(default_factory=list)
    paragraph: int = 0


TERMINAL = ".?!…。？！"
ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e",
    "u.s", "u.k", "a.m", "p.m", "no", "fig", "inc", "ltd", "co", "dept", "est",
    "approx", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec", "mt", "ft", "oz", "lb", "hr", "min", "sec", "vol",
}
CLOSERS = "\"'”’)]}"


def strip_wrappers(token: str) -> str:
    t = token.strip()
    while t and t[-1] in CLOSERS:
        t = t[:-1]
    return t.strip()


def is_terminal(token: str) -> bool:
    t = strip_wrappers(token)
    if not t or t[-1] not in TERMINAL:
        return False
    core = t.rstrip(TERMINAL).strip().lower()
    if core in ABBREV:
        return False
    # single capital initial, e.g. "J." or "A."
    if len(core) == 1 and core.isalpha() and core.isupper():
        return False
    # numbered list "1." at start of a line: keep as terminal (rare in ASR)
    return True


def is_comma(token: str) -> bool:
    t = strip_wrappers(token)
    return bool(t) and t[-1] in ",;:，；："


def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.!?;:…])", r"\1", s)
    return s


STOP_END = {
    "the", "a", "an", "of", "to", "in", "on", "at", "by", "for", "with", "and", "or", "but",
    "that", "which", "who", "whom", "whose", "when", "while", "as", "than", "from", "into",
    "over", "under", "between", "during", "because", "if", "so", "about", "after", "before",
    "through", "is", "are", "was", "were", "be", "been", "being", "has", "have", "had",
    "do", "does", "did", "will", "would", "can", "could", "should", "may", "might", "must",
    "not", "no", "very", "more", "most", "this", "these", "those", "his", "her", "their",
    "our", "your", "my", "its", "i", "you", "he", "she", "it", "we", "they", "there",
    "some", "any", "many", "much", "few", "several", "all", "both", "each", "every",
    "either", "neither", "another", "other", "such", "what", "how", "why", "where", "whether",
}


def is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af"


def filter_noise_words(words: list[Word]) -> tuple[list[Word], int]:
    """Drop hallucinated foreign-script tokens and long repeated-character runs."""
    kept: list[Word] = []
    dropped = 0
    for w in words:
        t = w.text.strip()
        if not t:
            dropped += 1
            continue
        cjk = sum(1 for c in t if is_cjk(c))
        if cjk and cjk / max(1, len(t)) >= 0.34:
            dropped += 1
            continue
        run, best = 1, 1
        for a, b in zip(t, t[1:]):
            run = run + 1 if a == b else 1
            best = max(best, run)
        if best >= 6 and best / len(t) > 0.6:
            dropped += 1
            continue
        kept.append(w)

    # A low-confidence word stranded before a long silence is usually a hallucination
    # whose timestamp leaked to the end of the previous chunk. Keep the text, move it
    # next to the following word so the sentence starts in the right place.
    for i in range(len(kept) - 1):
        w, nxt = kept[i], kept[i + 1]
        gap = nxt.start - w.end
        if gap > 2.0 and w.prob < 0.3 and nxt.text.strip()[:1].islower():
            new_end = nxt.start
            new_start = new_end - max(0.25, min(0.8, len(w.text.strip()) * 0.08))
            if i > 0:
                new_start = max(new_start, kept[i - 1].end)
            w.start, w.end = new_start, new_end

    return kept, dropped


def cleanup_labels(sents: list[dict]) -> None:
    """Drop stray exam labels such as a leading 'A.' or a trailing question number '1.'."""
    for s in sents:
        ws = s["words"]
        changed = False
        while len(ws) > 1 and re.fullmatch(r"[A-Z]\.", ws[0]["text"].strip()) \
                and ws[1]["text"].strip()[:1].isupper():
            ws.pop(0)
            changed = True
        while len(ws) > 1 and re.fullmatch(r"\d{1,3}\.", ws[-1]["text"].strip()):
            ws.pop()
            changed = True
        if changed and ws:
            s["text"] = clean_text("".join(w["text"] for w in ws))
            s["start"] = ws[0]["start"]
            s["end"] = ws[-1]["end"]
            s["avg_prob"] = round(sum(w["prob"] for w in ws) / len(ws), 3)



# --------------------------------------------------------------------------------------
# ASR
# --------------------------------------------------------------------------------------
MODEL_REPOS = {
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base.en": "Systran/faster-whisper-base.en",
    "small.en": "Systran/faster-whisper-small.en",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v3": "Systran/faster-whisper-large-v3",
}
MODEL_FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]

_DLL_DIRS: list = []


def setup_cuda_dlls() -> bool:
    """On Windows, CTranslate2 needs cuBLAS 12 DLLs. Reuse CUDA toolkit / torch / nvidia-pip copies."""
    if sys.platform != "win32":
        return True
    import site

    candidates: list[Path] = []
    bundled = os.environ.get("LISTENING_CUDA_DIR")
    if bundled:
        candidates.append(Path(bundled))
    for var in ("CUDA_PATH", "CUDA_HOME"):
        p = os.environ.get(var)
        if p:
            candidates.append(Path(p) / "bin")
    for base in {sys.base_prefix, sys.prefix}:
        candidates.append(Path(base) / "Lib" / "site-packages" / "torch" / "lib")
    try:
        for sp in site.getsitepackages():
            candidates.extend((Path(sp) / "nvidia").glob("*/bin"))
    except Exception:
        pass

    found = False
    for c in candidates:
        try:
            if not (c / "cublas64_12.dll").exists():
                continue
            _DLL_DIRS.append(os.add_dll_directory(str(c)))
            os.environ["PATH"] = str(c) + os.pathsep + os.environ.get("PATH", "")
            found = True
        except Exception:
            continue
    return found



def ensure_model(model_size: str, models_dir: Path) -> Path:
    """Download a CT2 model from the HF mirror without huggingface_hub (avoids system-proxy issues)."""
    dest = models_dir / model_size.replace("/", "_")
    dest.mkdir(parents=True, exist_ok=True)
    repo = MODEL_REPOS.get(model_size, model_size if "/" in model_size else f"Systran/faster-whisper-{model_size}")
    base = f"https://hf-mirror.com/{repo}/resolve/main/"

    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    for name in MODEL_FILES:
        target = dest / name
        if target.exists() and target.stat().st_size > 1024:
            continue
        part = target.with_suffix(target.suffix + ".part")
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(base + name, headers={"User-Agent": "listening-spike/0.1"})
                with opener.open(req, timeout=60) as resp:
                    total = int(resp.headers.get("Content-Length") or 0)
                    done = 0
                    t0 = time.time()
                    with open(part, "wb") as fh:
                        while True:
                            chunk = resp.read(1 << 20)
                            if not chunk:
                                break
                            fh.write(chunk)
                            done += len(chunk)
                            if total:
                                pct = done / total * 100
                                speed = done / max(0.001, time.time() - t0) / 1e6
                                sys.stdout.write(f"\r  download {name} {pct:5.1f}%  {done/1e6:7.1f}/{total/1e6:7.1f} MB  {speed:5.1f} MB/s")
                                sys.stdout.flush()
                    sys.stdout.write("\n")
                part.replace(target)
                break
            except Exception as exc:
                sys.stdout.write(f"\n  download {name} attempt {attempt} failed: {type(exc).__name__}: {exc}\n")
                if attempt == 3:
                    raise
                time.sleep(2)
    return dest


def _sanitize_words(words: list[Word]) -> list[Word]:
    cleaned: list[Word] = []
    prev_end = 0.0
    for w in words:
        if w.end <= w.start:
            continue
        if w.start < prev_end - 0.5:  # tolerate small overlap, clamp big inversions
            w.start = prev_end
        if w.end <= w.start:
            continue
        cleaned.append(w)
        prev_end = max(prev_end, w.end)
    return cleaned


def _quiet_cut(audio, s: float, e: float, target: float) -> float:
    """Pick the lowest-energy 50 ms frame near `target` (all in seconds)."""
    import numpy as np

    sr = 16000
    win = int(0.05 * sr)
    lo = max(int(s * sr) + win, int(target * sr) - 2 * sr)
    hi = min(int(e * sr) - win, int(target * sr) + 2 * sr)
    best_i, best_e = int(target * sr), None
    for i in range(lo, hi, win):
        frame = audio[i:i + win].astype(np.float64)
        energy = float((frame * frame).mean())
        if best_e is None or energy < best_e:
            best_e, best_i = energy, i
    return best_i / sr


def _plan_speech_chunks(audio, duration: float, max_chunk: float = 28.0,
                        pad: float = 0.55, edge_overlap: float = 0.3,
                        max_gap: float = 0.5) -> list[tuple[float, float]]:
    """Merge Silero speech regions into <=28 s, speech-dense chunks; split at gaps / silence."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    regions = get_speech_timestamps(
        audio, VadOptions(min_silence_duration_ms=300, speech_pad_ms=80))
    if not regions:
        return [(0.0, duration)]

    spans = [(r["start"] / 16000.0, r["end"] / 16000.0) for r in regions]
    chunks: list[tuple[float, float]] = []
    cur_s = max(0.0, spans[0][0] - pad)
    cur_e = min(duration, spans[0][1] + pad)
    prev_e = spans[0][1]
    for s, e in spans[1:]:
        gap = s - prev_e
        if gap > max_gap or (min(duration, e + pad) - cur_s) > max_chunk:
            chunks.append((cur_s, cur_e))
            cur_s = max(0.0, s - pad)
            cur_e = min(duration, e + pad)
        else:
            cur_e = min(duration, e + pad)
        prev_e = e
    chunks.append((cur_s, cur_e))

    out: list[tuple[float, float]] = []
    for s, e in chunks:
        while e - s > max_chunk:
            cut = _quiet_cut(audio, s, e, s + max_chunk - 2.0)
            if cut <= s + 4 or cut >= e - 4:
                break
            out.append((s, min(e, cut + edge_overlap)))
            s = max(s, cut - edge_overlap)
        out.append((s, e))
    return out


def _transcribe_chunks(model, audio_path: Path, language: str, progress: bool = True):
    """VAD-chunked ASR with padding + a rolling text prompt (light-weight interleaved context)."""
    from faster_whisper.audio import decode_audio

    audio = decode_audio(str(audio_path), sampling_rate=16000)
    duration = len(audio) / 16000.0
    chunks = _plan_speech_chunks(audio, duration)

    words: list[Word] = []
    raw: list[dict] = []
    last_end = 0.0
    prev_chunk_end = -99.0
    prompt: str | None = None

    for ci, (s, e) in enumerate(chunks):
        use_prompt = prompt if (prompt and s - prev_chunk_end < 0.25) else None
        segs, _ = model.transcribe(
            audio[int(s * 16000):int(e * 16000)],
            language=language,
            beam_size=5,
            word_timestamps=True,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=use_prompt,
            temperature=[0.0, 0.2, 0.4],
        )
        cw: list[Word] = []
        texts: list[str] = []
        for seg in segs:
            texts.append(seg.text)
            for w in (seg.words or []):
                if w.start is None or w.end is None:
                    continue
                cw.append(Word(text=w.word, start=float(w.start) + s, end=float(w.end) + s,
                               prob=float(getattr(w, "probability", 1.0) or 1.0), seg=ci))
        chunk_text = "".join(texts).strip()

        # Whisper sometimes echoes the prompt at the start of the next chunk.
        if use_prompt and cw:
            def norm(x: str) -> str:
                return re.sub(r"[^\w']", "", x.lower())

            pt = use_prompt.split()
            ct = [x.text.strip() for x in cw]
            for k in range(min(10, len(pt), len(ct)), 2, -1):
                if [norm(x) for x in ct[:k]] == [norm(x) for x in pt[-k:]]:
                    cw = cw[k:]
                    break

        for w in cw:
            if w.end <= w.start:
                continue
            if w.start < last_end - 0.2:  # duplicate from the overlap window
                if words and re.sub(r"[^\w']", "", words[-1].text.lower()) == \
                        re.sub(r"[^\w']", "", w.text.lower()) and w.prob > words[-1].prob:
                    words[-1] = w
                    last_end = max(last_end, w.end)
                continue
            words.append(w)
            last_end = max(last_end, w.end)

        raw.append({"id": ci, "start": round(s, 3), "end": round(e, 3), "text": chunk_text})
        if chunk_text:
            prompt = chunk_text[-160:]
        prev_chunk_end = e
        if progress:
            pct = (ci + 1) / len(chunks) * 100.0
            sys.stdout.write(f"\r  ASR(chunked) {pct:5.1f}%  chunk={ci + 1}/{len(chunks)} "
                             f"words={len(words):5d}  {e:7.1f}/{duration:7.1f}s")
            sys.stdout.flush()
    if progress:
        sys.stdout.write("\n")
    return _sanitize_words(words), raw, duration


def _probe_duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return float(p.stdout.strip().splitlines()[0])
    except Exception:
        return 0.0


def _silence_midpoints(path: Path) -> list[float]:
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", p.stderr)]
    return [(s + e) / 2 for s, e in zip(starts, ends) if e > s]


def _plan_long_chunks(duration: float, silences: list[float],
                      target: float = 720.0, overlap: float = 1.5) -> list[tuple[float, float]]:
    cuts: list[float] = []
    pos = 0.0
    while duration - pos > target * 1.5:
        desired = pos + target
        cand = [m for m in silences if desired - 90 <= m <= desired + 90]
        cut = min(cand, key=lambda x: abs(x - desired)) if cand else desired
        cuts.append(min(max(cut, pos + 60), duration - 60))
        pos = cuts[-1]
    chunks: list[tuple[float, float]] = []
    prev = 0.0
    for c in cuts:
        chunks.append((max(0.0, prev - overlap), min(duration, c + overlap)))
        prev = c
    chunks.append((max(0.0, prev - overlap), duration))
    return chunks


def _cut_wav(src: Path, dst: Path, start: float, length: float) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                    "-i", str(src), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
                   check=True)


def run_asr_long(model, audio: Path, work: Path, language: str = "en",
                 model_key: str = "fast", progress_cb=None) -> dict:
    """Sequential, resumable transcription for long audio (chunks cut at silence)."""
    t0 = time.time()
    duration = _probe_duration(audio)
    silences = _silence_midpoints(audio)
    chunks = _plan_long_chunks(duration, silences)
    cache_dir = work / f"long_{model_key}"
    cache_dir.mkdir(parents=True, exist_ok=True)

    words: list[Word] = []
    raw: list[dict] = []
    last_end = 0.0
    for i, (s, e) in enumerate(chunks):
        cache = cache_dir / f"chunk_{i:03d}.json"
        if cache.exists():
            part = json.loads(cache.read_text(encoding="utf-8"))
        else:
            tmp = cache_dir / f"chunk_{i:03d}.wav"
            _cut_wav(audio, tmp, s, max(1.0, e - s))
            segs, _ = model.transcribe(
                str(tmp), language=language, beam_size=5, word_timestamps=True,
                vad_filter=True, condition_on_previous_text=False,
                temperature=[0.0, 0.2, 0.4])
            part = []
            for seg in segs:
                for w in (seg.words or []):
                    if w.start is None or w.end is None:
                        continue
                    part.append({"text": w.word, "start": float(w.start), "end": float(w.end),
                                 "prob": float(getattr(w, "probability", 1.0) or 1.0)})
            cache.write_text(json.dumps(part, ensure_ascii=False), encoding="utf-8")
            tmp.unlink(missing_ok=True)
        for w in part:
            st, en = w["start"] + s, w["end"] + s
            if st < last_end - 0.15:
                continue
            words.append(Word(text=w["text"], start=st, end=en, prob=w.get("prob", 1.0), seg=i))
            last_end = max(last_end, en)
        raw.append({"id": i, "start": s, "end": e, "text": ""})
        if progress_cb:
            progress_cb(i + 1, len(chunks))

    return {
        "audio": str(audio),
        "model": model_key,
        "device": "",
        "compute_type": "",
        "language": language,
        "duration": duration,
        "asr_seconds": round(time.time() - t0, 2),
        "segments": raw,
        "words": [asdict(w) for w in _sanitize_words(words)],
    }


def run_asr(audio: Path, out_dir: Path, model_size: str, device: str, compute_type: str,
            language: str = "en", progress: bool = True, chunked: bool = False,
            model=None) -> dict:
    from faster_whisper import WhisperModel  # imported lazily so `seg`/`report` work without it

    t0 = time.time()
    if device == "auto":
        device, compute_type = ("cuda", "float16") if setup_cuda_dlls() else ("cpu", "int8")
    elif device.startswith("cuda") and not setup_cuda_dlls():
        print("  [warn] cuBLAS 12 DLL not found, falling back to CPU int8")
        device, compute_type = "cpu", "int8"

    if model is None:
        model_path = Path(model_size)
        if not model_path.exists():
            model_path = ensure_model(model_size, out_dir / "models")
        model = WhisperModel(str(model_path), device=device, compute_type=compute_type)

    if chunked:
        words, raw_segments, duration = _transcribe_chunks(model, audio, language, progress)
        return {
            "audio": str(audio),
            "model": model_size,
            "device": device,
            "compute_type": compute_type,
            "language": language,
            "duration": duration,
            "asr_seconds": round(time.time() - t0, 2),
            "segments": raw_segments,
            "words": [asdict(w) for w in words],
        }

    seg_iter, info = model.transcribe(
        str(audio),
        language=language,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        temperature=[0.0, 0.2, 0.4],
    )

    duration = float(info.duration or 0.0)
    raw_segments: list[dict] = []
    words: list[Word] = []
    seg_index = 0
    last_report = 0.0

    for seg in seg_iter:
        seg_index += 1
        ws: list[Word] = []
        for w in (seg.words or []):
            text = w.word
            if not text or w.start is None or w.end is None:
                continue
            ws.append(Word(text=text, start=float(w.start), end=float(w.end),
                           prob=float(getattr(w, "probability", 1.0) or 1.0), seg=seg_index))
        if not ws:
            continue
        raw_segments.append({
            "id": seg_index,
            "start": float(seg.start),
            "end": float(seg.end),
            "text": seg.text.strip(),
        })
        words.extend(ws)
        if progress and duration:
            pct = min(100.0, seg.end / duration * 100.0)
            if pct - last_report >= 1.0 or seg.end >= duration:
                last_report = pct
                sys.stdout.write(f"\r  ASR {pct:5.1f}%  seg={seg_index:4d}  words={len(words):6d}  {seg.end:7.1f}/{duration:7.1f}s")
                sys.stdout.flush()

    if progress:
        sys.stdout.write("\n")

    # sanitize: monotonic starts/ends, drop empties
    cleaned: list[Word] = []
    prev_end = 0.0
    for w in words:
        if w.end <= w.start:
            continue
        if w.start < prev_end - 0.5:  # tolerate small overlap, clamp big inversions
            w.start = prev_end
        if w.end <= w.start:
            continue
        cleaned.append(w)
        prev_end = max(prev_end, w.end)
    words = cleaned

    return {
        "audio": str(audio),
        "model": model_size,
        "device": device,
        "compute_type": compute_type,
        "language": language,
        "duration": duration,
        "asr_seconds": round(time.time() - t0, 2),
        "segments": raw_segments,
        "words": [asdict(w) for w in words],
    }


# --------------------------------------------------------------------------------------
# segmentation: DP over words
# --------------------------------------------------------------------------------------
def segment(words: list[Word], min_words: int = 3, max_words: int = 45,
            min_dur: float = 0.6, max_dur: float = 18.0) -> list[Sentence]:
    n = len(words)
    if n == 0:
        return []

    gaps = [max(0.0, words[i + 1].start - words[i].end) for i in range(n - 1)]
    sorted_gaps = sorted(gaps)
    p75 = sorted_gaps[int(len(sorted_gaps) * 0.75)] if sorted_gaps else 0.5
    pause_ref = max(0.25, min(0.9, p75))

    def boundary_reward(j: int) -> float:
        """reward for putting a boundary after word j-1 (i.e. segment ends at j)."""
        if j >= n:
            return 0.0
        prev, nxt = words[j - 1], words[j]
        prev_t = strip_wrappers(prev.text)
        nxt_t = nxt.text.strip()
        gap = max(0.0, nxt.start - prev.end)
        terminal = is_terminal(prev.text)

        r = 3.2 if terminal else 0.0
        r += 1.9 * min(1.0, gap / pause_ref)
        if nxt_t[:1].isupper():
            r += 0.25
        if prev.seg != nxt.seg:
            r += 0.1

        if not terminal:
            if nxt_t[:1].islower():
                r -= 2.6
            if prev_t.lower().strip(".,!?;:'\"") in STOP_END:
                r -= 2.2
            if is_comma(prev.text):
                nxt_core = strip_wrappers(nxt_t)
                if (len(nxt_core) == 1 and nxt_core.isupper()) or nxt_core.lower() in {"and", "or", "nor"}:
                    r -= 2.5
                elif gap < 0.55:
                    r -= 1.4
                else:
                    r -= 0.3
        elif nxt_t[:1].islower():
            r -= 0.8
        return r

    NEG = float("-inf")
    dp = [NEG] * (n + 1)
    back = [-1] * (n + 1)
    dp[0] = 0.0

    for j in range(1, n + 1):
        best, best_i = NEG, -1
        lo = max(0, j - max_words)
        for i in range(lo, j):
            if dp[i] == NEG:
                continue
            cnt = j - i
            if cnt < min_words:
                continue
            dur = words[j - 1].end - words[i].start
            if dur < min_dur:
                continue
            if dur > max_dur:
                continue
            # segment cost: fixed cost keeps segment count sane
            cost = -1.0
            if j < n:
                cost += boundary_reward(j)
            else:
                cost += 3.0  # reward ending at the very end
            if dur > 15.0:
                cost -= 0.12 * (dur - 15.0)
            if cnt > 35:
                cost -= 0.15 * (cnt - 35)
            total = dp[i] + cost
            if total > best:
                best, best_i = total, i
        dp[j], back[j] = best, best_i

    if dp[n] == NEG:  # constraints too tight; relax once
        if n <= 2:
            ws = words
            return [Sentence(idx=0, start=ws[0].start, end=ws[-1].end,
                             text=clean_text("".join(w.text for w in ws)), words=ws,
                             avg_prob=sum(w.prob for w in ws) / len(ws),
                             terminal=is_terminal(ws[-1].text))]
        return segment(words, 2, max(60, n), 0.0, max(30.0, words[-1].end - words[0].start))

    cuts = []
    j = n
    while j > 0:
        i = back[j]
        if i < 0:
            i = 0
        cuts.append((i, j))
        j = i
    cuts.reverse()

    sentences: list[Sentence] = []
    for k, (i, j) in enumerate(cuts):
        ws = words[i:j]
        text = clean_text("".join(w.text for w in ws))
        avg = sum(w.prob for w in ws) / len(ws)
        terminal = is_terminal(ws[-1].text)
        pause_after = (words[j].start - ws[-1].end) if j < n else 0.0
        whisper_break = (j < n and ws[-1].seg != words[j].seg)
        s = Sentence(
            idx=k,
            start=ws[0].start,
            end=ws[-1].end,
            text=text,
            words=ws,
            avg_prob=avg,
            terminal=terminal,
            pause_after=pause_after,
            whisper_break=whisper_break,
        )
        dur = s.end - s.start
        if not terminal and pause_after < 0.2:
            s.flags.append("no-punct-no-pause")
        if dur > 15:
            s.flags.append("too-long")
        if dur < 1.0:
            s.flags.append("too-short")
        if avg < 0.55:
            s.flags.append("low-conf")
        sentences.append(s)

    # paragraph grouping: long pause or too many sentences
    para = 0
    for k, s in enumerate(sentences):
        if k > 0:
            gap = s.start - sentences[k - 1].end
            if gap >= 1.8 or (k % 10 == 0):
                para += 1
        s.paragraph = para

    return sentences


def load_words(payload: dict) -> list[Word]:
    return [Word(**{k: v for k, v in w.items() if k in Word.__annotations__}) for w in payload["words"]]


def sentences_to_json(sentences: list[Sentence]) -> list[dict]:
    out = []
    for s in sentences:
        out.append({
            "idx": s.idx,
            "start": round(s.start, 3),
            "end": round(s.end, 3),
            "text": s.text,
            "avg_prob": round(s.avg_prob, 3),
            "terminal": s.terminal,
            "pause_after": round(s.pause_after, 3),
            "whisper_break": s.whisper_break,
            "flags": s.flags,
            "paragraph": s.paragraph,
            "words": [{"text": w.text, "start": round(w.start, 3), "end": round(w.end, 3),
                       "prob": round(w.prob, 3)} for w in s.words],
        })
    return out


# --------------------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------------------
def fmt_ts(t: float) -> str:
    m = int(t // 60)
    s = t - m * 60
    return f"{m:02d}:{s:06.3f}"


def build_html(payload: dict, sentences: list[dict], audio_name: str, out_path: Path) -> None:
    esc = html.escape

    rows = []
    for s in sentences:
        cls = ["s"]
        if s["flags"]:
            cls.append("warn")
        words_html = "".join(
            f'<span class="w{" low" if w["prob"] < 0.55 else ""}" title="{w["prob"]:.2f}">{esc(w["text"])}</span>'
            for w in s["words"]
        )
        badges = "".join(f'<span class="badge">{esc(f)}</span>' for f in s["flags"])
        rows.append(
            f'<div class="{" ".join(cls)}" data-i="{s["idx"]}" data-start="{s["start"]}" data-end="{s["end"]}">'
            f'<div class="gutter"><span class="num">#{s["idx"] + 1}</span>'
            f'<span class="ts">{fmt_ts(s["start"])}</span>'
            f'<span class="dur">{s["end"] - s["start"]:.1f}s</span></div>'
            f'<div class="body"><div class="text">{words_html}</div>'
            f'<div class="sub">{badges}<span class="p">conf {s["avg_prob"]:.2f}</span></div></div>'
            f'<button class="flag" title="标记这句有问题">⚑</button>'
            f'</div>'
        )

    data_json = json.dumps(sentences, ensure_ascii=False)
    total = len(sentences)
    warn = sum(1 for s in sentences if s["flags"])
    duration = payload.get("duration", 0)
    asr_sec = payload.get("asr_seconds", 0)

    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>分句验证 · {esc(audio_name)}</title>
<style>
:root {{ --bg:#0f1216; --panel:#161b22; --line:#252c36; --fg:#e6edf3; --dim:#8b949e; --acc:#4aa8ff; --warn:#e3b341; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 "Segoe UI",system-ui,sans-serif; }}
header {{ position:sticky; top:0; z-index:5; background:rgba(15,18,22,.96); border-bottom:1px solid var(--line); padding:12px 20px; backdrop-filter:blur(6px); }}
h1 {{ margin:0 0 4px; font-size:16px; font-weight:600; }}
.meta {{ color:var(--dim); font-size:12.5px; }}
.controls {{ display:flex; gap:8px; align-items:center; margin-top:10px; flex-wrap:wrap; }}
button, select {{ background:var(--panel); color:var(--fg); border:1px solid var(--line); border-radius:8px; padding:7px 12px; font-size:13px; cursor:pointer; }}
button:hover {{ border-color:var(--acc); }}
button.primary {{ background:var(--acc); color:#04121f; border-color:var(--acc); font-weight:600; }}
#now {{ color:var(--dim); font-variant-numeric:tabular-nums; font-size:12.5px; }}
main {{ max-width:1080px; margin:0 auto; padding:12px 20px 120px; }}
.s {{ display:flex; gap:12px; padding:10px 10px; border-radius:10px; border:1px solid transparent; cursor:pointer; }}
.s:hover {{ background:var(--panel); }}
.s.active {{ background:#12253a; border-color:#2d5b8a; }}
.s.warn .num {{ color:var(--warn); }}
.gutter {{ width:92px; flex:none; display:flex; flex-direction:column; color:var(--dim); font-size:11.5px; font-variant-numeric:tabular-nums; }}
.gutter .num {{ color:var(--fg); font-size:12.5px; }}
.body {{ flex:1; min-width:0; }}
.text {{ font-size:16px; }}
.w.low {{ border-bottom:2px dotted #d29922; }}
.sub {{ margin-top:3px; display:flex; gap:8px; align-items:center; color:var(--dim); font-size:11.5px; }}
.badge {{ background:#3a2d12; color:var(--warn); border-radius:4px; padding:0 5px; }}
.flag {{ opacity:.25; padding:4px 8px; align-self:flex-start; }}
.s:hover .flag {{ opacity:.8; }}
.s.flagged .flag {{ opacity:1; color:#f85149; border-color:#f85149; }}
body.playing .s.active {{ box-shadow:inset 3px 0 0 var(--acc); }}
footer {{ position:fixed; bottom:0; left:0; right:0; background:rgba(15,18,22,.97); border-top:1px solid var(--line); padding:10px 20px; display:flex; gap:12px; align-items:center; }}
#bar {{ flex:1; height:6px; background:#232a34; border-radius:3px; position:relative; cursor:pointer; }}
#barfill {{ position:absolute; left:0; top:0; bottom:0; width:0; background:var(--acc); border-radius:3px; }}
#feedback {{ position:fixed; right:16px; bottom:70px; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px; display:none; max-width:420px; font-size:12.5px; white-space:pre-wrap; }}
kbd {{ background:#222a35; border:1px solid var(--line); border-radius:4px; padding:0 5px; font-size:11px; }}
</style></head>
<body>
<header>
  <h1>分句验证报告 · {esc(audio_name)}</h1>
  <div class="meta">{total} 句 · 时长 {fmt_ts(duration)} · {esc(str(payload.get("model")))} / {esc(str(payload.get("device")))} · 转写耗时 {asr_sec:.0f}s · 自动标记可疑 {warn} 句</div>
  <div class="controls">
    <button class="primary" id="play">▶ 播放</button>
    <button id="prev">↑ 上一句</button>
    <button id="next">↓ 下一句</button>
    <label><input type="checkbox" id="loop"> 单句循环</label>
    <label><input type="checkbox" id="follow" checked> 自动滚动</label>
    <select id="rate"><option>0.75</option><option selected>1</option><option>1.25</option><option>1.5</option></select>
    <span id="now">点击任意句子试听；空格播放/暂停</span>
  </div>
</header>
<main id="list">
{"".join(rows)}
</main>
<footer>
  <button id="fprev">⏮</button><button id="ftoggle">⏯</button><button id="fnext">⏭</button>
  <span id="time" style="font-variant-numeric:tabular-nums;color:var(--dim);font-size:12px">{fmt_ts(0)} / {fmt_ts(duration)}</span>
  <div id="bar"><div id="barfill"></div></div>
  <button id="export">导出问题清单 (<span id="flagcount">0</span>)</button>
</footer>
<div id="feedback"></div>
<audio id="audio" preload="auto" src="{esc(audio_name)}"></audio>
<script>
const S = {data_json};
const A = document.getElementById('audio');
const list = document.getElementById('list');
let cur = -1, mode = 'idle', raf = 0;
const loopEl = document.getElementById('loop');
const followEl = document.getElementById('follow');
const flagged = new Set(JSON.parse(localStorage.getItem('spike_flags_{esc(audio_name)}') || '[]'));

function saveFlags(){{ localStorage.setItem('spike_flags_{esc(audio_name)}', JSON.stringify([...flagged])); }}
function paintFlags(){{
  document.querySelectorAll('.s').forEach(el => el.classList.toggle('flagged', flagged.has(+el.dataset.i)));
  document.getElementById('flagcount').textContent = flagged.size;
}}
function highlight(i){{
  document.querySelectorAll('.s.active').forEach(e => e.classList.remove('active'));
  if(i < 0) return;
  const el = document.querySelector(`.s[data-i="${{i}}"]`);
  if(!el) return;
  el.classList.add('active');
  document.body.classList.add('playing');
  if(followEl.checked) el.scrollIntoView({{block:'center', behavior:'smooth'}});
}}
function playSentence(i, loopOne){{
  if(i < 0 || i >= S.length) return;
  cur = i; mode = loopOne ? 'loop-one' : 'sentence';
  const start = Math.max(0, S[i].start - 0.05);
  const go = () => {{ A.currentTime = start; A.play().catch(()=>{{}}); highlight(i); }};
  if(A.readyState >= 1) go(); else A.addEventListener('loadedmetadata', go, {{once:true}});
}}
function findAt(t){{
  let lo = 0, hi = S.length - 1, ans = -1;
  while(lo <= hi){{ const mid = (lo + hi) >> 1; if(S[mid].start <= t){{ ans = mid; lo = mid + 1; }} else hi = mid - 1; }}
  return ans;
}}
function tick(){{
  const t = A.currentTime;
  document.getElementById('barfill').style.width = (t / A.duration * 100) + '%';
  document.getElementById('time').textContent = fmt(t) + ' / ' + fmt(A.duration || {duration});
  if(mode === 'sentence' || mode === 'loop-one'){{
    if(cur >= 0 && t >= S[cur].end - 0.01){{
      if(mode === 'loop-one' || loopEl.checked){{ A.currentTime = Math.max(0, S[cur].start - 0.02); }}
      else {{ A.pause(); mode = 'idle'; }}
    }}
  }} else if(mode === 'continuous'){{
    const i = findAt(t);
    if(i !== cur){{ cur = i; highlight(i); }}
  }}
  raf = requestAnimationFrame(tick);
}}
function fmt(t){{ t = Math.max(0, t || 0); const m = Math.floor(t / 60); const s = t - m * 60; return String(m).padStart(2,'0') + ':' + s.toFixed(1).padStart(4,'0'); }}

document.getElementById('list').addEventListener('click', e => {{
  const flag = e.target.closest('.flag');
  const row = e.target.closest('.s');
  if(!row) return;
  const i = +row.dataset.i;
  if(flag){{ e.stopPropagation(); flagged.has(i) ? flagged.delete(i) : flagged.add(i); saveFlags(); paintFlags(); return; }}
  playSentence(i, false);
}});
document.getElementById('play').onclick = () => {{
  if(!A.paused){{ A.pause(); mode = 'idle'; return; }}
  if(mode === 'idle' && cur < 0) cur = 0;
  if(cur >= 0){{ A.currentTime = Math.max(0, S[cur].start - 0.05); }}
  mode = 'continuous'; A.play();
}};
document.getElementById('prev').onclick = () => playSentence(Math.max(0, (cur < 0 ? 0 : cur) - 1), false);
document.getElementById('next').onclick = () => playSentence(Math.min(S.length - 1, cur + 1), false);
document.getElementById('ftoggle').onclick = () => document.getElementById('play').click();
document.getElementById('fprev').onclick = () => document.getElementById('prev').click();
document.getElementById('fnext').onclick = () => document.getElementById('next').click();
document.getElementById('rate').onchange = e => A.playbackRate = +e.target.value;
document.getElementById('bar').onclick = e => {{
  const r = e.currentTarget.getBoundingClientRect();
  A.currentTime = (e.clientX - r.left) / r.width * A.duration;
  mode = 'continuous'; A.play();
}};
document.addEventListener('keydown', e => {{
  if(e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if(e.code === 'Space'){{ e.preventDefault(); document.getElementById('play').click(); }}
  if(e.code === 'ArrowDown'){{ e.preventDefault(); document.getElementById('next').click(); }}
  if(e.code === 'ArrowUp'){{ e.preventDefault(); document.getElementById('prev').click(); }}
  if(e.code === 'KeyL'){{ loopEl.checked = !loopEl.checked; }}
  if(e.code === 'KeyF' && cur >= 0){{ flagged.has(cur) ? flagged.delete(cur) : flagged.add(cur); saveFlags(); paintFlags(); }}
}});
document.getElementById('export').onclick = () => {{
  const lines = [...flagged].sort((a,b)=>a-b).map(i => `#${{i+1}} [${{fmt(S[i].start)}} - ${{fmt(S[i].end)}}] ${{S[i].text}}`);
  const txt = lines.length ? lines.join('\\n') : '(未标记任何句子)';
  const show = () => {{
    const f = document.getElementById('feedback');
    f.style.display = 'block';
    f.textContent = '问题清单（可直接复制）：\\n' + txt;
    setTimeout(() => f.style.display = 'none', 15000);
  }};
  if(navigator.clipboard && navigator.clipboard.writeText){{
    navigator.clipboard.writeText(txt).then(show, () => {{
      const ta = document.createElement('textarea');
      ta.value = txt; document.body.appendChild(ta); ta.select();
      try {{ document.execCommand('copy'); }} catch(e) {{}}
      document.body.removeChild(ta); show();
    }});
  }} else {{ show(); }}
}};
paintFlags();
tick();
</script>
</body></html>"""
    out_path.write_text(page, encoding="utf-8")


# --------------------------------------------------------------------------------------
# targeted re-transcription of suspect sentences
# --------------------------------------------------------------------------------------
def _load_whisper_for(payload: dict, out: Path):
    from faster_whisper import WhisperModel

    device = payload.get("device", "cpu")
    compute = payload.get("compute_type", "int8")
    if device.startswith("cuda") and not setup_cuda_dlls():
        device, compute = "cpu", "int8"
    model_name = payload.get("model", "small.en")
    model_path = Path(model_name)
    if not model_path.exists():
        model_path = out / "models" / model_name
    return WhisperModel(str(model_path), device=device, compute_type=compute)


def _suspect_spans(sents: list[Sentence], duration: float,
                   pad: float = 0.55, max_span: float = 30.0) -> list[tuple[float, float, float, float]]:
    items: list[tuple[float, float, float, float]] = []
    for s in sents:
        gaps = [s.words[i + 1].start - s.words[i].end for i in range(len(s.words) - 1)]
        max_gap = max(gaps) if gaps else 0.0
        suspect = ("no-punct-no-pause" in s.flags) or (s.end - s.start > 12.0) \
            or (max_gap > 1.2) or (s.avg_prob < 0.5)
        if not suspect:
            continue
        a, b = max(0.0, s.start - pad), min(duration, s.end + pad)
        if b - a <= max_span:
            items.append((a, b, s.start, s.end))
    items.sort()
    merged: list[tuple[float, float, float, float]] = []
    for a, b, ss, ee in items:
        if merged and a <= merged[-1][1] + 0.05:
            pa, pb, pss, pee = merged[-1]
            merged[-1] = (pa, max(pb, b), min(pss, ss), max(pee, ee))
        else:
            merged.append((a, b, ss, ee))
    return [x for x in merged if x[1] - x[0] <= max_span]


def _append_boundary_words(words: list[Word], model, audio, language: str,
                           duration: float) -> tuple[list[Word], int]:
    """Recover a word that was cut at a chunk boundary (e.g. the missing 'City.')."""
    sents = segment(words)
    added = 0
    for i, s in enumerate(sents):
        if s.terminal:
            continue
        nxt_start = sents[i + 1].start if i + 1 < len(sents) else min(duration, s.end + 1.5)
        if nxt_start - s.end < 0.15:
            continue
        a = max(0.0, s.end - 1.5)
        b = min(duration, s.end + 1.2)
        segs, _ = model.transcribe(
            audio[int(a * 16000):int(b * 16000)],
            language=language,
            beam_size=5,
            word_timestamps=True,
            vad_filter=False,
            condition_on_previous_text=False,
            temperature=[0.0, 0.2, 0.4],
        )
        new: list[Word] = []
        for seg in segs:
            for w in (seg.words or []):
                if w.start is None or w.end is None:
                    continue
                st, en = float(w.start) + a, float(w.end) + a
                if not (s.end - 0.1 <= st < nxt_start - 0.05):
                    continue
                if en <= st:  # Whisper collapses a trailing word to zero length
                    en = min(nxt_start - 0.02, st + 0.35)
                new.append(Word(text=w.word, start=st, end=en,
                                prob=float(getattr(w, "probability", 1.0) or 1.0), seg=0))
        text = "".join(w.text for w in new).strip()
        if new and text[-1:] in TERMINAL:
            words = _sanitize_words(sorted(words + new, key=lambda x: (x.start, x.end)))
            added += 1
    return words, added


def repair_transcript(words: list[Word], model, audio, language: str, duration: float,
                      progress: bool = True, max_spans: int = 25):
    """Re-transcribe only suspect sentences with surrounding context and splice the words back."""
    sents = segment(words)
    spans = _suspect_spans(sents, duration)[:max_spans]
    if not spans:
        return words, []

    repairs = []
    for a, b, ss, ee in spans:
        segs, _ = model.transcribe(
            audio[int(a * 16000):int(b * 16000)],
            language=language,
            beam_size=5,
            word_timestamps=True,
            vad_filter=False,
            condition_on_previous_text=False,
            temperature=[0.0, 0.2, 0.4],
        )
        new: list[Word] = []
        for seg in segs:
            for w in (seg.words or []):
                if w.start is None or w.end is None:
                    continue
                st, en = float(w.start) + a, float(w.end) + a
                if ss - 0.25 <= (st + en) / 2 <= ee + 0.25:
                    new.append(Word(text=w.word, start=st, end=en,
                                    prob=float(getattr(w, "probability", 1.0) or 1.0), seg=0))
        if not new:
            continue

        old = [w for w in words if ss - 0.1 <= (w.start + w.end) / 2 <= ee + 0.1]
        if not old:
            continue
        old_text = "".join(w.text for w in old)
        new_text = "".join(w.text for w in new)
        old_caps = {w.strip(".,!?;:\"'()") for w in old_text.split() if w[:1].isupper()}
        new_caps = {w.strip(".,!?;:\"'()") for w in new_text.split() if w[:1].isupper()}
        if len(old_caps - new_caps) >= 2:  # re-transcription lost several proper nouns -> reject
            continue
        old_term = old_text.rstrip()[-1:] in TERMINAL
        new_term = new_text.rstrip()[-1:] in TERMINAL
        if (old_term and not new_term) or (not old_term and not new_term):
            continue

        keep = [w for w in words if not (ss - 0.1 <= (w.start + w.end) / 2 <= ee + 0.1)]
        words = _sanitize_words(sorted(keep + new, key=lambda x: (x.start, x.end)))
        repairs.append({"start": round(ss, 2), "end": round(ee, 2), "words": len(new)})
        if progress:
            print(f"    repair {ss:7.1f}-{ee:7.1f}s -> {len(new)} words")

    words, added = _append_boundary_words(words, model, audio, language, duration)
    if progress and added:
        print(f"    boundary repair -> {added} sentence(s)")
    return words, repairs


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------
def cmd_all(args) -> None:
    audio = Path(args.audio).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    # copy the playable audio next to the report
    play_name = "audio" + audio.suffix.lower()
    play_path = out / play_name
    if not play_path.exists():
        shutil.copy2(audio, play_path)
    print(f"[1/4] audio: {audio.name} ({audio.stat().st_size/1e6:.1f} MB)")

    payload_path = out / "transcript.json"
    if args.reuse and payload_path.exists():
        print("[2/4] reuse existing transcript.json")
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    else:
        print(f"[2/4] ASR model={args.model} device={args.device} compute={args.compute_type}")
        payload = run_asr(audio, out, args.model, args.device, args.compute_type, args.language,
                          chunked=args.chunked)
        payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"  words={len(payload['words'])} segments={len(payload['segments'])} asr={payload['asr_seconds']}s")

    print("[3/4] DP segmentation")
    words = load_words(payload)
    words, dropped = filter_noise_words(words)
    if dropped:
        print(f"  filtered {dropped} hallucinated/non-English tokens")

    if not args.no_repair and not payload.get("repaired"):
        try:
            from faster_whisper.audio import decode_audio

            print("  targeted re-transcription of suspect sentences ...")
            model = _load_whisper_for(payload, out)
            wav = decode_audio(str(audio), sampling_rate=16000)
            words, repairs = repair_transcript(
                words, model, wav, payload.get("language", "en"),
                payload.get("duration", len(wav) / 16000.0))
            if repairs:
                payload["words"] = [asdict(w) for w in words]
                payload["repaired"] = True
                payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                print(f"  repaired {len(repairs)} span(s)")
            else:
                print("  no suspect spans")
        except Exception as exc:
            print(f"  repair skipped: {type(exc).__name__}: {exc}")

    sents = segment(words, args.min_words, args.max_words, args.min_dur, args.max_dur)
    sents_json = sentences_to_json(sents)
    cleanup_labels(sents_json)
    (out / "sentences.json").write_text(json.dumps(sents_json, ensure_ascii=False, indent=1), encoding="utf-8")
    durs = [s["end"] - s["start"] for s in sents_json]
    punct = sum(1 for s in sents_json if s["terminal"])
    print(f"  sentences={len(sents_json)} median={sorted(durs)[len(durs)//2]:.2f}s "
          f"flags={sum(1 for s in sents_json if s['flags'])} "
          f"punctuated={punct}/{len(sents_json)}")

    print("[4/4] report")
    build_html(payload, sents_json, play_name, out / "report.html")
    print(f"  -> {out / 'report.html'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("all")
    p.add_argument("--audio", required=True)
    p.add_argument("--out", default="out")
    p.add_argument("--model", default="small.en")
    p.add_argument("--device", default="cuda")
    p.add_argument("--compute-type", default="float16")
    p.add_argument("--language", default="en")
    p.add_argument("--min-words", type=int, default=3)
    p.add_argument("--max-words", type=int, default=45)
    p.add_argument("--min-dur", type=float, default=0.6)
    p.add_argument("--max-dur", type=float, default=18.0)
    p.add_argument("--reuse", action="store_true")
    p.add_argument("--no-repair", action="store_true", help="skip targeted re-transcription")
    p.add_argument("--chunked", action="store_true", help="experimental: VAD chunks + rolling prompt")
    p.set_defaults(func=cmd_all)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
