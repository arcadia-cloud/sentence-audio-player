"""SQLite persistence for the listening app."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("LISTENING_CONFIG") or (APP_ROOT / "config.json"))


def _resolve_data_dir() -> Path:
    env = os.environ.get("LISTENING_DATA")
    if env:
        return Path(env)
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            p = cfg.get("data_dir")
            if p:
                return Path(p).expanduser()
        except Exception:
            pass
    return APP_ROOT / "data"


DATA = _resolve_data_dir()
DB_PATH = DATA / "app.db"


def save_data_dir(path: str) -> None:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["data_dir"] = str(path)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def cursor():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with cursor() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                filename TEXT NOT NULL,
                ext TEXT,
                duration REAL DEFAULT 0,
                audio_path TEXT,
                peaks TEXT,
                status TEXT DEFAULT 'uploaded',
                progress REAL DEFAULT 0,
                stage TEXT,
                quality TEXT DEFAULT 'fast',
                error TEXT,
                transcript_id TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS transcripts (
                id TEXT PRIMARY KEY,
                media_id TEXT NOT NULL,
                model TEXT,
                language TEXT DEFAULT 'en',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS segments (
                id TEXT PRIMARY KEY,
                transcript_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                start REAL NOT NULL,
                end REAL NOT NULL,
                text TEXT NOT NULL,
                verified INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                start REAL NOT NULL,
                end REAL NOT NULL,
                text TEXT NOT NULL,
                prob REAL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_segments_tr ON segments(transcript_id, idx);
            CREATE INDEX IF NOT EXISTS idx_words_seg ON words(segment_id, idx);
            """
        )
        cols = {r["name"] for r in c.execute("PRAGMA table_info(media)")}
        if "stage" not in cols:
            c.execute("ALTER TABLE media ADD COLUMN stage TEXT")
        if "quality" not in cols:
            c.execute("ALTER TABLE media ADD COLUMN quality TEXT DEFAULT 'fast'")


# ----------------------------------------------------------------------------- media
def list_media() -> list[dict]:
    with cursor() as c:
        rows = c.execute(
            """SELECT m.*, (SELECT COUNT(*) FROM segments s WHERE s.transcript_id = m.transcript_id) AS seg_count
               FROM media m ORDER BY m.created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_media(mid: str) -> dict | None:
    with cursor() as c:
        row = c.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
    return dict(row) if row else None


def create_media(mid: str, title: str, filename: str, ext: str, duration: float,
                 audio_path: str, peaks: list[float]) -> None:
    with cursor() as c:
        c.execute(
            """INSERT INTO media(id,title,filename,ext,duration,audio_path,peaks,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (mid, title, filename, ext, duration, audio_path,
             json.dumps(peaks, separators=(",", ":")), "uploaded", now()),
        )


def update_media(mid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with cursor() as c:
        c.execute(f"UPDATE media SET {cols} WHERE id=?", (*fields.values(), mid))


def delete_media(mid: str) -> None:
    m = get_media(mid)
    if not m:
        return
    with cursor() as c:
        if m.get("transcript_id"):
            c.execute("DELETE FROM words WHERE segment_id IN (SELECT id FROM segments WHERE transcript_id=?)",
                      (m["transcript_id"],))
            c.execute("DELETE FROM segments WHERE transcript_id=?", (m["transcript_id"],))
            c.execute("DELETE FROM transcripts WHERE id=?", (m["transcript_id"],))
        c.execute("DELETE FROM media WHERE id=?", (mid,))


# ------------------------------------------------------------------------- transcript
def replace_transcript(media_id: str, model: str, language: str, sentences: list[dict]) -> str:
    tid = uuid.uuid4().hex[:12]
    with cursor() as c:
        old = c.execute("SELECT transcript_id FROM media WHERE id=?", (media_id,)).fetchone()
        if old and old["transcript_id"]:
            c.execute("DELETE FROM words WHERE segment_id IN (SELECT id FROM segments WHERE transcript_id=?)",
                      (old["transcript_id"],))
            c.execute("DELETE FROM segments WHERE transcript_id=?", (old["transcript_id"],))
            c.execute("DELETE FROM transcripts WHERE id=?", (old["transcript_id"],))

        c.execute("INSERT INTO transcripts(id,media_id,model,language,created_at) VALUES(?,?,?,?,?)",
                  (tid, media_id, model, language, now()))
        for i, s in enumerate(sentences):
            sid = uuid.uuid4().hex[:12]
            c.execute("INSERT INTO segments(id,transcript_id,idx,start,end,text,verified) VALUES(?,?,?,?,?,?,?)",
                      (sid, tid, i, float(s["start"]), float(s["end"]), s["text"],
                       1 if s.get("verified") else 0))
            c.executemany(
                "INSERT INTO words(segment_id,idx,start,end,text,prob) VALUES(?,?,?,?,?,?)",
                [(sid, j, float(w["start"]), float(w["end"]), w["text"], float(w.get("prob", 1.0)))
                 for j, w in enumerate(s.get("words", []))],
            )
        c.execute("UPDATE media SET transcript_id=?, status='ready', progress=1, error=NULL WHERE id=?",
                  (tid, media_id))
    return tid


def get_transcript(media_id: str) -> dict | None:
    m = get_media(media_id)
    if not m or not m.get("transcript_id"):
        return None
    tid = m["transcript_id"]
    with cursor() as c:
        tr = c.execute("SELECT * FROM transcripts WHERE id=?", (tid,)).fetchone()
        segs = c.execute("SELECT * FROM segments WHERE transcript_id=? ORDER BY idx", (tid,)).fetchall()
        words = c.execute(
            """SELECT w.* FROM words w JOIN segments s ON s.id=w.segment_id
               WHERE s.transcript_id=? ORDER BY w.segment_id, w.idx""", (tid,)).fetchall()
    by_seg: dict[str, list[dict]] = {}
    for w in words:
        by_seg.setdefault(w["segment_id"], []).append(
            {"start": w["start"], "end": w["end"], "text": w["text"], "prob": w["prob"]})
    out_segments = []
    for s in segs:
        out_segments.append({
            "id": s["id"], "idx": s["idx"], "start": s["start"], "end": s["end"],
            "text": s["text"], "verified": bool(s["verified"]),
            "words": by_seg.get(s["id"], []),
        })
    return {
        "transcript_id": tid, "media_id": media_id,
        "model": tr["model"] if tr else "", "language": tr["language"] if tr else "en",
        "duration": m["duration"], "segments": out_segments,
    }


def get_media_by_segment(sid: str) -> dict | None:
    with cursor() as c:
        row = c.execute(
            """SELECT m.* FROM media m
               JOIN transcripts t ON t.id = m.transcript_id
               JOIN segments s ON s.transcript_id = t.id
               WHERE s.id = ?""", (sid,)).fetchone()
    return dict(row) if row else None


def get_segment(sid: str) -> dict | None:
    with cursor() as c:
        s = c.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
        if not s:
            return None
        ws = c.execute("SELECT * FROM words WHERE segment_id=? ORDER BY idx", (sid,)).fetchall()
    return {**dict(s), "words": [dict(w) for w in ws]}


def update_segment(sid: str, *, text: str | None = None, verified: bool | None = None,
                   start: float | None = None, end: float | None = None) -> None:
    fields: dict = {}
    if text is not None:
        fields["text"] = text.strip()
    if verified is not None:
        fields["verified"] = 1 if verified else 0
    if start is not None:
        fields["start"] = float(start)
    if end is not None:
        fields["end"] = float(end)
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with cursor() as c:
        c.execute(f"UPDATE segments SET {cols} WHERE id=?", (*fields.values(), sid))


def split_segment(sid: str, word_index: int) -> None:
    seg = get_segment(sid)
    if not seg:
        return
    ws = seg["words"]
    if not (1 <= word_index < len(ws)):
        return
    left, right = ws[:word_index], ws[word_index:]
    new_id = uuid.uuid4().hex[:12]
    left_text = "".join(w["text"] for w in left).strip()
    right_text = "".join(w["text"] for w in right).strip()
    with cursor() as c:
        c.execute("UPDATE segments SET end=?, text=? WHERE id=?",
                  (left[-1]["end"], left_text, sid))
        c.execute("UPDATE segments SET idx=idx+1 WHERE transcript_id=? AND idx>?",
                  (seg["transcript_id"], seg["idx"]))
        c.execute("INSERT INTO segments(id,transcript_id,idx,start,end,text,verified) VALUES(?,?,?,?,?,?,0)",
                  (new_id, seg["transcript_id"], seg["idx"] + 1, right[0]["start"],
                   right[-1]["end"], right_text))
        c.execute("UPDATE words SET segment_id=?, idx=idx-? WHERE segment_id=? AND idx>=?",
                  (new_id, word_index, sid, word_index))


def merge_with_next(sid: str) -> None:
    seg = get_segment(sid)
    if not seg:
        return
    with cursor() as c:
        nxt = c.execute("SELECT * FROM segments WHERE transcript_id=? AND idx=?",
                        (seg["transcript_id"], seg["idx"] + 1)).fetchone()
        if not nxt:
            return
        text = (seg["text"].rstrip() + " " + nxt["text"].lstrip()).strip()
        c.execute("UPDATE segments SET end=?, text=? WHERE id=?",
                  (max(seg["end"], nxt["end"]), text, sid))
        c.execute("UPDATE words SET segment_id=? WHERE segment_id=?",
                  (sid, nxt["id"]))
        # renumber moved words
        rows = c.execute("SELECT id FROM words WHERE segment_id=? ORDER BY id", (sid,)).fetchall()
        for i, r in enumerate(rows):
            c.execute("UPDATE words SET idx=? WHERE id=?", (i, r["id"]))
        c.execute("DELETE FROM segments WHERE id=?", (nxt["id"],))
        c.execute("UPDATE segments SET idx=idx-1 WHERE transcript_id=? AND idx>?",
                  (seg["transcript_id"], nxt["idx"]))


def replace_segment_words(sid: str, words: list[dict], text: str, start: float, end: float) -> None:
    with cursor() as c:
        c.execute("DELETE FROM words WHERE segment_id=?", (sid,))
        c.executemany(
            "INSERT INTO words(segment_id,idx,start,end,text,prob) VALUES(?,?,?,?,?,?)",
            [(sid, i, float(w["start"]), float(w["end"]), w["text"], float(w.get("prob", 1.0)))
             for i, w in enumerate(words)],
        )
        c.execute("UPDATE segments SET text=?, start=?, end=? WHERE id=?", (text, start, end, sid))
