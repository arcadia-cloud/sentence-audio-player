"""Background transcription worker with quality levels and long-audio chunking."""
from __future__ import annotations

import os
import threading
import traceback
from pathlib import Path

from . import audio, core, db

WORKSPACE = Path(__file__).resolve().parents[3]
_SPIKE_MODEL = WORKSPACE / "listening-spike" / "out" / "models" / "small.en"
_MODEL_OVERRIDE = os.environ.get("LISTENING_MODEL")

MODEL = _MODEL_OVERRIDE or (str(_SPIKE_MODEL) if _SPIKE_MODEL.exists() else "small.en")
DEVICE = os.environ.get("LISTENING_DEVICE", "cuda")
COMPUTE = os.environ.get("LISTENING_COMPUTE", "float16")
LANGUAGE = os.environ.get("LISTENING_LANGUAGE", "en")

LONG_SECONDS = float(os.environ.get("LISTENING_LONG_SECONDS", 45 * 60))

QUALITY = {
    "fast": ("small.en", "快速"),
    "standard": ("medium.en", "标准"),
    "high": ("large-v3", "高精度"),
}

_job_lock = threading.Lock()
_model_lock = threading.Lock()
_models: dict[str, object] = {}


def model_name(quality: str) -> str:
    return QUALITY.get(quality, QUALITY["fast"])[0]


def get_model(quality: str = "fast"):
    """Shared faster-whisper model per quality level."""
    quality = quality if quality in QUALITY else "fast"
    with _model_lock:
        if quality in _models:
            return _models[quality]
        from faster_whisper import WhisperModel

        device, compute = DEVICE, COMPUTE
        if device == "auto":
            device, compute = ("cuda", "float16") if core.setup_cuda_dlls() else ("cpu", "int8")
        elif device.startswith("cuda") and not core.setup_cuda_dlls():
            device, compute = "cpu", "int8"

        if quality == "fast" and Path(MODEL).exists():
            path = Path(MODEL)
        else:
            name = model_name(quality)
            path = db.DATA / "models" / name
            if not path.exists():
                path = core.ensure_model(name, db.DATA / "models")
        try:
            model = WhisperModel(str(path), device=device, compute_type=compute)
        except Exception as exc:
            print(f"[gpu] cuda init failed: {exc}", flush=True)
            if not str(device).startswith("cuda"):
                raise
            model = WhisperModel(str(path), device="cpu", compute_type="int8")
        _models[quality] = model
        return model


def _avg_prob(seg: dict) -> float:
    ws = seg.get("words") or []
    return sum(float(w.get("prob", 1.0)) for w in ws) / len(ws) if ws else 1.0


def _improve_low_confidence(media_id: str, quality: str, audio_path: Path) -> tuple[int, int]:
    """Re-transcribe low-confidence sentences with the next quality tier."""
    target = {"fast": "standard", "standard": "high"}.get(quality)
    if not target:
        return 0, 0
    tr = db.get_transcript(media_id)
    if not tr:
        return 0, 0
    cands = [s for s in tr["segments"]
             if len(s.get("text", "").split()) >= 2 and _avg_prob(s) < 0.62][:12]
    if not cands:
        return 0, 0

    model = get_model(target)
    ctx = 0.35
    changed = 0
    for i, s in enumerate(cands):
        try:
            start = max(0.0, s["start"] - ctx)
            dur = (s["end"] - s["start"]) + 2 * ctx
            tmp = db.DATA / "work" / f"improve_{s['id']}.wav"
            audio.cut_wav(audio_path, tmp, start, dur)
            segs, _ = model.transcribe(
                str(tmp), language=LANGUAGE, beam_size=5, word_timestamps=True,
                vad_filter=False, condition_on_previous_text=False,
                temperature=[0.0, 0.2, 0.4])
            words = []
            for sg in segs:
                for w in (sg.words or []):
                    if w.start is None or w.end is None:
                        continue
                    st, en = float(w.start) + start, float(w.end) + start
                    if en < s["start"] - 0.1 or st > s["end"] + 0.1:
                        continue
                    words.append({"text": w.word, "start": st, "end": en,
                                  "prob": float(getattr(w, "probability", 1.0) or 1.0)})
            tmp.unlink(missing_ok=True)
            if not words:
                continue
            newavg = sum(w["prob"] for w in words) / len(words)
            newtext = "".join(w["text"] for w in words).strip()
            if newavg > _avg_prob(s) + 0.02 and len(newtext) >= len(s["text"]) * 0.6:
                db.replace_segment_words(s["id"], words, newtext, s["start"], s["end"])
                changed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[improve] {s.get('id')} failed: {exc}")
        db.update_media(media_id, progress=0.95 + 0.05 * (i + 1) / len(cands),
                        stage=f"低置信句重跑 {i + 1}/{len(cands)}")
    return len(cands), changed


def improve_now(media_id: str, quality: str | None = None) -> dict:
    """Improve an already transcribed file in place (one-shot button)."""
    if not _job_lock.acquire(blocking=False):
        return {"busy": True}
    try:
        media = db.get_media(media_id)
        if not media or not media.get("audio_path"):
            raise RuntimeError("media or audio missing")
        q = quality or media.get("quality") or "fast"
        target = {"fast": "standard", "standard": "high"}.get(q)
        if not target:
            return {"total": 0, "changed": 0, "target": None, "quality": q}
        total, changed = _improve_low_confidence(media_id, q, Path(media["audio_path"]))
        db.update_media(media_id, status="ready", progress=1, stage=None)
        return {"total": total, "changed": changed, "target": model_name(target), "quality": q}
    finally:
        _job_lock.release()


def _transcribe(media_id: str, quality: str, improve: bool = False) -> None:
    try:
        media = db.get_media(media_id)
        if not media or not media.get("audio_path"):
            raise RuntimeError("media or audio missing")
        audio_path = Path(media["audio_path"])
        work = db.DATA / "work" / media_id
        work.mkdir(parents=True, exist_ok=True)
        name = model_name(quality)
        duration = float(media.get("duration") or 0.0)

        db.update_media(media_id, quality=quality, status="transcribing", progress=0.02,
                        stage=f"加载模型 {name}", error=None)
        model = get_model(quality)

        long_mode = duration > LONG_SECONDS
        repair = duration < 3 * 3600

        if long_mode:
            total_chunks = 0

            def on_chunk(done: int, total: int) -> None:
                nonlocal total_chunks
                total_chunks = total
                db.update_media(media_id,
                                progress=0.55 * done / max(1, total),
                                stage=f"分块转写 {done}/{total}")

            payload = core.run_asr_long(model, audio_path, work, LANGUAGE, quality, on_chunk)
            db.update_media(media_id, progress=0.55, stage="合并分块")
        else:
            payload = core.run_asr(audio_path, work, name, DEVICE, COMPUTE, LANGUAGE,
                                   progress=False, model=model)
            db.update_media(media_id, progress=0.55, stage="分句")

        words = core.load_words(payload)
        words, _ = core.filter_noise_words(words)

        if repair and core.segment(words):
            db.update_media(media_id, progress=0.65, stage="局部校对")
            from faster_whisper.audio import decode_audio
            wav = decode_audio(str(audio_path), sampling_rate=16000)
            words, _ = core.repair_transcript(words, model, wav, LANGUAGE,
                                             float(payload["duration"]))
        db.update_media(media_id, progress=0.92, stage="整理文本")

        sentences = core.segment(words)
        data = core.sentences_to_json(sentences)
        core.cleanup_labels(data)
        db.replace_transcript(media_id, name, LANGUAGE, data)
        if improve and quality != "high":
            db.update_media(media_id, status="transcribing", progress=0.95, stage="低置信句重跑")
            _improve_low_confidence(media_id, quality, audio_path)
        db.update_media(media_id, status="ready", stage=None, progress=1.0)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        db.update_media(media_id, status="error", progress=0, stage=None,
                        error=f"{type(exc).__name__}: {exc}")
    finally:
        _job_lock.release()


def start(media_id: str, quality: str = "fast", improve: bool = False) -> bool:
    """Start a transcription job unless one is already running."""
    if quality not in QUALITY:
        quality = "fast"
    if not _job_lock.acquire(blocking=False):
        return False
    db.update_media(media_id, status="queued", progress=0, stage="排队中",
                    quality=quality, error=None)
    t = threading.Thread(target=_transcribe, args=(media_id, quality, improve), daemon=True)
    t.start()
    return True


def busy() -> bool:
    return _job_lock.locked()
