"""FastAPI application: upload, transcribe, edit, stream."""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import audio, db, worker

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = Path(os.environ.get("LISTENING_FRONTEND") or (ROOT / "frontend"))
ALLOWED = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    yield


app = FastAPI(title="Listening Studio", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

UNDO: dict[str, list[dict]] = {}


def push_undo(media_id: str | None, label: str = "编辑", detail: str = "") -> None:
    if not media_id:
        return
    data = db.get_transcript(media_id)
    if not data:
        return
    stack = UNDO.setdefault(media_id, [])
    stack.append({
        "label": label,
        "detail": detail,
        "time": time.strftime("%H:%M:%S"),
        "segments": data["segments"],
    })
    del stack[:-20]


def media_of_segment(sid: str) -> str | None:
    m = db.get_media_by_segment(sid)
    return m["id"] if m else None


def public_media(m: dict) -> dict:
    return {k: m[k] for k in ("id", "title", "filename", "duration", "status", "progress",
                             "stage", "quality", "error", "transcript_id", "created_at") if k in m} | {
        "seg_count": m.get("seg_count", 0)}


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/api/config")
async def config() -> dict:
    return {"model": Path(worker.MODEL).name, "device": worker.DEVICE, "language": worker.LANGUAGE}


@app.get("/api/media")
async def media_list() -> list[dict]:
    return [public_media(m) for m in db.list_media()]


@app.patch("/api/media/{mid}")
async def media_rename(mid: str, body: dict = Body(...)) -> dict:
    m = db.get_media(mid)
    if not m:
        raise HTTPException(404, "not found")
    title = str(body.get("title", "")).strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    db.update_media(mid, title=title[:120])
    return public_media(db.get_media(mid) or {})


@app.get("/api/storage")
async def storage_info() -> dict:
    return {"path": str(db.DATA), "config": str(db.CONFIG_PATH)}


@app.post("/api/open-folder")
async def open_folder() -> dict:
    db.DATA.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        try:
            os.startfile(str(db.DATA))  # noqa: S606
            return {"ok": True, "path": str(db.DATA), "via": "shell"}
        except Exception:
            pass
        try:
            subprocess.Popen(["explorer.exe", str(db.DATA)])
            return {"ok": True, "path": str(db.DATA), "via": "explorer"}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, "系统拒绝打开资源管理器。请点「复制路径」后手动打开。") from exc
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(db.DATA)])
        else:
            subprocess.Popen(["xdg-open", str(db.DATA)])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"无法打开目录: {exc}") from exc
    return {"ok": True, "path": str(db.DATA)}


@app.post("/api/storage")
async def set_storage(body: dict = Body(...)) -> dict:
    raw = str(body.get("path", "")).strip().strip('"')
    if not raw:
        raise HTTPException(400, "路径不能为空")
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise HTTPException(400, "请填写绝对路径，例如 D:\\ListeningData")
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / "test.tmp").write_text("ok", encoding="utf-8")
        (target / "test.tmp").unlink()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"目录不可写: {exc}") from exc

    if target.resolve() == db.DATA.resolve():
        return {"ok": True, "path": str(target), "changed": False, "restart_required": False}

    for name in ("uploads", "audio", "work"):
        src = db.DATA / name
        if src.exists():
            shutil.copytree(src, target / name, dirs_exist_ok=True)
    src_db = db.DB_PATH
    if src_db.exists():
        src = sqlite3.connect(src_db)
        dst = sqlite3.connect(target / "app.db")
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    db.save_data_dir(str(target))
    return {"ok": True, "path": str(target), "changed": True, "restart_required": True}


@app.post("/api/media")
async def media_upload(file: UploadFile = File(...)) -> dict:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"unsupported file type: {ext}")
    mid = uuid.uuid4().hex[:12]
    raw = db.DATA / "uploads" / f"{mid}{ext}"
    wav = db.DATA / "audio" / f"{mid}.wav"
    raw.parent.mkdir(parents=True, exist_ok=True)
    with open(raw, "wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    try:
        audio.extract_wav(raw, wav)
        duration = audio.probe_duration(wav)
        peaks = audio.compute_peaks(wav)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"音频解码失败: {exc}") from exc
    title = Path(file.filename or "material").stem
    db.create_media(mid, title, file.filename or raw.name, ext, duration, str(wav), peaks)
    return public_media(db.get_media(mid) or {})


@app.delete("/api/media/{mid}")
async def media_delete(mid: str) -> dict:
    m = db.get_media(mid)
    if not m:
        raise HTTPException(404, "not found")
    db.delete_media(mid)
    for p in (m.get("audio_path"), db.DATA / "uploads" / f"{mid}{m.get('ext', '')}"):
        try:
            if p:
                Path(p).unlink(missing_ok=True)
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/media/{mid}/transcribe")
async def transcribe(mid: str, body: dict = Body(default={})) -> dict:
    m = db.get_media(mid)
    if not m:
        raise HTTPException(404, "not found")
    quality = str(body.get("quality") or m.get("quality") or "fast")
    if quality not in ("fast", "standard", "high"):
        raise HTTPException(400, "invalid quality")
    if worker.busy():
        raise HTTPException(409, "another transcription is running")
    improve = bool(body.get("improve"))
    if not worker.start(mid, quality, improve):
        raise HTTPException(409, "another transcription is running")
    return {"ok": True, "quality": quality, "improve": improve}


@app.get("/api/media/{mid}/status")
async def status(mid: str) -> dict:
    m = db.get_media(mid)
    if not m:
        raise HTTPException(404, "not found")
    return public_media(m)


@app.get("/api/media/{mid}/transcript")
async def transcript(mid: str) -> dict:
    data = db.get_transcript(mid)
    if not data:
        raise HTTPException(404, "transcript not ready")
    return data


@app.post("/api/media/{mid}/undo")
async def undo(mid: str, body: dict = Body(default={})) -> dict:
    stack = UNDO.get(mid)
    if not stack:
        raise HTTPException(400, "没有可撤销的操作")
    steps = max(1, int(body.get("steps", 1)))
    steps = min(steps, len(stack))
    popped = [stack.pop() for _ in range(steps)]
    entry = popped[-1]
    label = entry.get("label", "编辑")
    tr = db.get_transcript(mid)
    model = (tr or {}).get("model") or "small.en"
    db.replace_transcript(mid, model, "en", entry["segments"])
    return {"ok": True, "label": label, "steps": steps,
            "transcript": db.get_transcript(mid) or {}}


@app.get("/api/media/{mid}/undo-info")
async def undo_info(mid: str) -> dict:
    stack = UNDO.get(mid) or []
    history = [
        {"label": e.get("label", "编辑"), "detail": e.get("detail", ""), "time": e.get("time", "")}
        for e in reversed(stack)
    ]
    return {"count": len(stack), "label": stack[-1]["label"] if stack else None,
            "history": history}


@app.post("/api/media/{mid}/improve")
async def improve(mid: str) -> dict:
    m = db.get_media(mid)
    if not m:
        raise HTTPException(404, "not found")
    if worker.busy():
        raise HTTPException(409, "另一个转写任务正在运行")
    push_undo(mid, "低置信句重跑", "整篇")
    result = await run_in_threadpool(worker.improve_now, mid)
    if result.get("busy"):
        raise HTTPException(409, "另一个转写任务正在运行")
    return result


@app.get("/api/media/{mid}/peaks")
async def peaks(mid: str) -> dict:
    m = db.get_media(mid)
    if not m:
        raise HTTPException(404, "not found")
    return {"peaks": m.get("peaks") or "[]"}


def _range_response(path: Path, request: Request) -> StreamingResponse | FileResponse:
    size = path.stat().st_size
    header = request.headers.get("range")
    if not header:
        return FileResponse(path, media_type="audio/wav", headers={"Accept-Ranges": "bytes"})
    m = re.match(r"bytes=(\d*)-(\d*)", header)
    if not m:
        raise HTTPException(416, "bad range")
    start = int(m.group(1)) if m.group(1) else 0
    end = int(m.group(2)) if m.group(2) else size - 1
    end = min(end, size - 1)
    if start > end:
        raise HTTPException(416, "bad range")
    length = end - start + 1

    def iterator():
        with open(path, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "no-store",
    }
    return StreamingResponse(iterator(), status_code=206, media_type="audio/wav", headers=headers)


@app.get("/api/media/{mid}/audio")
async def media_audio(mid: str, request: Request):
    m = db.get_media(mid)
    if not m or not m.get("audio_path"):
        raise HTTPException(404, "not found")
    return _range_response(Path(m["audio_path"]), request)


@app.patch("/api/segments/{sid}")
async def patch_segment(sid: str, body: dict = Body(...)) -> dict:
    seg = db.get_segment(sid)
    if not seg:
        raise HTTPException(404, "not found")
    if body.get("text") is not None:
        label = "修改文本"
    elif body.get("verified") is not None:
        label = "标记核对"
    elif body.get("start") is not None or body.get("end") is not None:
        label = "调整边界"
    else:
        label = "编辑句子"
    push_undo(media_of_segment(sid), label, f"第 {seg['idx'] + 1} 句")
    db.update_segment(
        sid,
        text=body.get("text"),
        verified=body.get("verified"),
        start=body.get("start"),
        end=body.get("end"),
    )
    return db.get_segment(sid) or {}


@app.post("/api/segments/{sid}/split")
async def split_segment(sid: str, body: dict = Body(...)) -> dict:
    idx = int(body.get("word_index", 0))
    seg = db.get_segment(sid)
    detail = f"第 {seg['idx'] + 1} 句" if seg else ""
    push_undo(media_of_segment(sid), "拆分句子", detail)
    db.split_segment(sid, idx)
    return {"ok": True}


@app.post("/api/segments/{sid}/merge")
async def merge_segment(sid: str) -> dict:
    seg = db.get_segment(sid)
    detail = f"第 {seg['idx'] + 1} 句" if seg else ""
    push_undo(media_of_segment(sid), "合并句子", detail)
    db.merge_with_next(sid)
    return {"ok": True}


@app.post("/api/segments/{sid}/verify")
async def verify_segment(sid: str, body: dict = Body(default={})) -> dict:
    seg = db.get_segment(sid)
    detail = f"第 {seg['idx'] + 1} 句" if seg else ""
    push_undo(media_of_segment(sid), "标记核对", detail)
    db.update_segment(sid, verified=bool(body.get("verified", True)))
    return {"ok": True}


@app.post("/api/segments/{sid}/retranscribe")
async def retranscribe(sid: str) -> dict:
    seg = db.get_segment(sid)
    if not seg:
        raise HTTPException(404, "not found")
    media = db.get_media_by_segment(sid)
    if not media:
        raise HTTPException(404, "media not found")
    push_undo(media["id"], "重转写句子", f"第 {seg['idx'] + 1} 句")
    start = max(0.0, seg["start"] - 0.35)
    dur = (seg["end"] - seg["start"]) + 0.7
    tmp = db.DATA / "work" / f"seg_{sid}.wav"
    audio.cut_wav(Path(media["audio_path"]), tmp, start, dur)
    model = worker.get_model(media.get("quality") or "fast")
    with worker._model_lock:  # CTranslate2 model is not thread-safe
        segs, _ = model.transcribe(str(tmp), language=worker.LANGUAGE, beam_size=5,
                                   word_timestamps=True, vad_filter=False,
                                   condition_on_previous_text=False,
                                   temperature=[0.0, 0.2, 0.4])
        words = []
        for s in segs:
            for w in (s.words or []):
                if w.start is None or w.end is None:
                    continue
                words.append({"text": w.word, "start": float(w.start) + start,
                              "end": float(w.end) + start,
                              "prob": float(getattr(w, "probability", 1.0) or 1.0)})
    tmp.unlink(missing_ok=True)
    if not words:
        raise HTTPException(422, "no speech recognized")
    text = re.sub(r"\s+", " ", "".join(w["text"] for w in words)).strip()
    db.replace_segment_words(sid, words, text, words[0]["start"], words[-1]["end"])
    return db.get_segment(sid) or {}


@app.get("/api/media/{mid}/export")
async def export(mid: str, fmt: str = "srt") -> FileResponse:
    data = db.get_transcript(mid)
    if not data:
        raise HTTPException(404, "transcript not ready")
    m = db.get_media(mid)
    base = re.sub(r"[^\w\-]+", "_", (m or {}).get("title", "transcript")).strip("_") or "transcript"
    out = db.DATA / "work" / f"{base}.{fmt}"
    segs = data["segments"]

    def ts(t: float) -> str:
        h = int(t // 3600)
        mnt = int((t % 3600) // 60)
        s = t % 60
        return f"{h:02d}:{mnt:02d}:{s:06.3f}".replace(".", ",")

    if fmt == "srt":
        lines = []
        for i, s in enumerate(segs, 1):
            lines.append(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{s['text']}\n")
        out.write_text("\n".join(lines), encoding="utf-8")
    elif fmt == "txt":
        out.write_text("\n".join(s["text"] for s in segs), encoding="utf-8")
    else:
        import json
        out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        out = out.with_suffix(".json")
    return FileResponse(out, filename=out.name)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8765, reload=False)
