import asyncio
import io
import json
import os
import wave
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiosqlite
import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from segmenter import SAMPLE_RATE, Segment, SpeechSegmenter

DB_PATH = os.environ.get("DB_PATH", "/data/liveflow.db")
ASR_BASE_URL = os.environ.get("ASR_BASE_URL", "http://asr:8000/v1").rstrip("/")
ASR_MODEL = os.environ.get("ASR_MODEL", "Qwen/Qwen3-ASR-1.7B")
ASR_API_KEY = os.environ.get("ASR_API_KEY", "sk-local")
ASR_LANGUAGE = os.environ.get("ASR_LANGUAGE", "").strip()

db: aiosqlite.Connection | None = None
http: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, http
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            t0 REAL NOT NULL,
            t1 REAL NOT NULL,
            text TEXT NOT NULL
        );
        """
    )
    await db.execute("PRAGMA foreign_keys = ON")
    await db.commit()
    http = httpx.AsyncClient(timeout=120)
    yield
    await http.aclose()
    await db.close()


app = FastAPI(title="LiveFlow", lifespan=lifespan)


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


async def transcribe(pcm: bytes) -> str:
    data = {"model": ASR_MODEL}
    if ASR_LANGUAGE:
        data["language"] = ASR_LANGUAGE
    resp = await http.post(
        f"{ASR_BASE_URL}/audio/transcriptions",
        headers={"Authorization": f"Bearer {ASR_API_KEY}"},
        data=data,
        files={"file": ("segment.wav", pcm_to_wav(pcm), "audio/wav")},
    )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


# ---------------------------------------------------------------- WebSocket

@app.websocket("/ws")
async def ws_transcribe(ws: WebSocket):
    await ws.accept()

    # Premier message : {"type": "start", "title": "..."}
    try:
        start = json.loads(await ws.receive_text())
        assert start.get("type") == "start"
    except Exception:
        await ws.close(code=4000)
        return

    title = (start.get("title") or "").strip() or datetime.now().strftime("Réunion du %d/%m/%Y %H:%M")
    cur = await db.execute(
        "INSERT INTO meetings (title, created_at) VALUES (?, ?)",
        (title, datetime.now(timezone.utc).isoformat()),
    )
    meeting_id = cur.lastrowid
    await db.commit()
    await ws.send_json({"type": "ready", "meeting_id": meeting_id, "title": title})

    segmenter = SpeechSegmenter()
    queue: asyncio.Queue[Segment | None] = asyncio.Queue()

    async def worker():
        """Transcrit les segments dans l'ordre et pousse le texte au client."""
        while True:
            seg = await queue.get()
            if seg is None:
                return
            try:
                text = await transcribe(seg.pcm)
            except Exception as exc:
                await ws.send_json({"type": "error", "message": f"Transcription échouée : {exc}"})
                continue
            if not text:
                continue
            await db.execute(
                "INSERT INTO segments (meeting_id, t0, t1, text) VALUES (?, ?, ?, ?)",
                (meeting_id, seg.t0, seg.t1, text),
            )
            await db.commit()
            await ws.send_json({"type": "segment", "t0": seg.t0, "t1": seg.t1, "text": text})

    worker_task = asyncio.create_task(worker())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                for seg in segmenter.feed(msg["bytes"]):
                    queue.put_nowait(seg)
            elif msg.get("text"):
                control = json.loads(msg["text"])
                if control.get("type") == "stop":
                    if (last := segmenter.flush()) is not None:
                        queue.put_nowait(last)
                    queue.put_nowait(None)
                    await worker_task
                    worker_task = None
                    await ws.send_json({"type": "done", "meeting_id": meeting_id})
                    break
    except WebSocketDisconnect:
        pass
    finally:
        if worker_task is not None:
            # Déconnexion brutale : on transcrit quand même ce qui restait.
            if (last := segmenter.flush()) is not None:
                queue.put_nowait(last)
            queue.put_nowait(None)
            try:
                await worker_task
            except Exception:
                pass


# --------------------------------------------------------------------- API

@app.get("/api/meetings")
async def list_meetings():
    rows = await db.execute_fetchall(
        """
        SELECT m.id, m.title, m.created_at, COUNT(s.id) AS segments,
               COALESCE(MAX(s.t1), 0) AS duration
        FROM meetings m LEFT JOIN segments s ON s.meeting_id = m.id
        GROUP BY m.id ORDER BY m.id DESC
        """
    )
    return [dict(r) for r in rows]


async def get_meeting_or_404(meeting_id: int) -> dict:
    cur = await db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, "Réunion introuvable")
    return dict(row)


@app.get("/api/meetings/{meeting_id}")
async def get_meeting(meeting_id: int):
    meeting = await get_meeting_or_404(meeting_id)
    rows = await db.execute_fetchall(
        "SELECT t0, t1, text FROM segments WHERE meeting_id = ? ORDER BY id", (meeting_id,)
    )
    meeting["segments"] = [dict(r) for r in rows]
    return meeting


@app.delete("/api/meetings/{meeting_id}")
async def delete_meeting(meeting_id: int):
    await get_meeting_or_404(meeting_id)
    await db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    await db.commit()
    return JSONResponse({"ok": True})


def fmt_ts(seconds: float, srt: bool = False) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if srt:
        ms = int(round((seconds - int(seconds)) * 1000))
        return f"{h:02}:{m:02}:{s:02},{ms:03}"
    return f"{h:02}:{m:02}:{s:02}"


@app.get("/api/meetings/{meeting_id}/export")
async def export_meeting(meeting_id: int, format: str = "txt"):
    meeting = await get_meeting(meeting_id)
    segs = meeting["segments"]
    title = meeting["title"]
    # Les en-têtes HTTP n'acceptent que l'ASCII : on translittère le titre.
    safe = "".join(
        c if c.isascii() and (c.isalnum() or c in " -_") else "_" for c in title
    ).strip() or "reunion"

    if format == "json":
        body, mime, ext = json.dumps(meeting, ensure_ascii=False, indent=2), "application/json", "json"
    elif format == "md":
        lines = [f"# {title}", ""]
        lines += [f"**[{fmt_ts(s['t0'])}]** {s['text']}" for s in segs]
        body, mime, ext = "\n\n".join(lines) + "\n", "text/markdown", "md"
    elif format == "srt":
        blocks = [
            f"{i}\n{fmt_ts(s['t0'], srt=True)} --> {fmt_ts(s['t1'], srt=True)}\n{s['text']}"
            for i, s in enumerate(segs, 1)
        ]
        body, mime, ext = "\n\n".join(blocks) + "\n", "application/x-subrip", "srt"
    elif format == "txt":
        body, mime, ext = "\n".join(s["text"] for s in segs) + "\n", "text/plain", "txt"
    else:
        raise HTTPException(400, "Format inconnu (txt, md, srt, json)")

    return Response(
        content=body,
        media_type=f"{mime}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}.{ext}"'},
    )


app.mount("/", StaticFiles(directory="static", html=True), name="static")
