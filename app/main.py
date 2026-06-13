import asyncio
import audioop
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
import wave
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiosqlite
import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from segmenter import SAMPLE_RATE, Segment, SpeechSegmenter

DB_PATH = os.environ.get("DB_PATH", "/data/liveflow.db")
ASR_BASE_URL = os.environ.get("ASR_BASE_URL", "http://asr:8000/v1").rstrip("/")
ASR_MODEL = os.environ.get("ASR_MODEL", "Qwen/Qwen3-ASR-1.7B")
ASR_API_KEY = os.environ.get("ASR_API_KEY", "sk-local")
ASR_LANGUAGE = os.environ.get("ASR_LANGUAGE", "").strip()

# Qwen3-ASR transcrit les sons non verbaux (hmm, toux...) en interjections
# chinoises et vLLM ignore le paramètre language : si la langue configurée
# n'est pas une langue CJK, on retire ces caractères des transcriptions.
CJK_RE = re.compile(r"[　-〿㐀-䶿一-鿿豈-﫿＀-￯]+")
STRIP_CJK = ASR_LANGUAGE.lower() not in ("", "zh", "ja", "ko", "yue")


def clean_transcript(text: str) -> str:
    if STRIP_CJK and CJK_RE.search(text):
        text = CJK_RE.sub(" ", text)
        text = re.sub(r"\s+", " ", text)
        text = text.lstrip(" 。，、！？.,;!?")  # ponctuation orpheline en tête
        text = text.rstrip(" 。，、！？")        # ponctuation chinoise en queue
    return text.strip()

# Diarisation (identification des locuteurs) : embeddings ECAPA-TDNN sur CPU
DIARIZATION = os.environ.get("DIARIZATION", "off").strip().lower() == "on"
DIARIZATION_THRESHOLD = float(os.environ.get("DIARIZATION_THRESHOLD", "0.35"))
DIARIZATION_MAX_SPEAKERS = int(os.environ.get("DIARIZATION_MAX_SPEAKERS", "8"))

LIVEFLOW_USER = os.environ.get("LIVEFLOW_USER", "admin")
LIVEFLOW_PASSWORD = os.environ.get("LIVEFLOW_PASSWORD", "admin")
SESSION_TTL = 7 * 24 * 3600  # 7 jours
SESSION_COOKIE = "liveflow_session"

db: aiosqlite.Connection | None = None
http: httpx.AsyncClient | None = None
session_secret: bytes = b""
embedding_model = None  # EmbeddingModel, chargé au démarrage si DIARIZATION=on


def load_session_secret() -> bytes:
    """Secret HMAC persistant pour signer les cookies de session."""
    path = os.path.join(os.path.dirname(DB_PATH), "session-secret")
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        secret = secrets.token_bytes(32)
        with open(path, "wb") as f:
            f.write(secret)
        return secret


def make_session_token() -> str:
    expiry = str(int(time.time()) + SESSION_TTL)
    sig = hmac.new(session_secret, expiry.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def session_valid(token: str) -> bool:
    try:
        expiry, sig = token.split(".", 1)
        expected = hmac.new(session_secret, expiry.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected) and time.time() < int(expiry)
    except (ValueError, AttributeError):
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, http, session_secret
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    session_secret = load_session_secret()
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
            text TEXT NOT NULL,
            speaker TEXT NOT NULL DEFAULT ''
        );
        """
    )
    # Migration : ajouter la colonne speaker aux bases existantes
    try:
        await db.execute("ALTER TABLE segments ADD COLUMN speaker TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass  # la colonne existe déjà
    await db.execute("PRAGMA foreign_keys = ON")
    await db.commit()

    global embedding_model
    if DIARIZATION and embedding_model is None:
        print("Chargement du modèle de diarisation ECAPA-TDNN...", flush=True)
        from diarizer import EmbeddingModel
        embedding_model = await asyncio.to_thread(EmbeddingModel)
        print("Modèle de diarisation prêt.", flush=True)
    http = httpx.AsyncClient(timeout=120)
    yield
    await http.aclose()
    await db.close()


app = FastAPI(title="LiveFlow", lifespan=lifespan)


# ----------------------------------------------------------- authentification

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    authed = session_valid(request.cookies.get(SESSION_COOKIE, ""))
    if path.startswith("/api") and path != "/api/login" and not authed:
        return JSONResponse({"detail": "Non authentifié"}, status_code=401)
    if path == "/" and not authed:
        return RedirectResponse("/login")
    if path == "/login" and authed:
        return RedirectResponse("/")
    return await call_next(request)


class LoginBody(BaseModel):
    username: str
    password: str


@app.get("/login")
async def login_page():
    return FileResponse("static/login.html")


@app.post("/api/login")
async def login(body: LoginBody):
    user_ok = hmac.compare_digest(body.username.encode(), LIVEFLOW_USER.encode())
    pass_ok = hmac.compare_digest(body.password.encode(), LIVEFLOW_PASSWORD.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Identifiants invalides")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        SESSION_COOKIE, make_session_token(),
        max_age=SESSION_TTL, httponly=True, samesite="lax",
    )
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


NORM_TARGET_PEAK = 22000   # niveau crête visé (≈0,67 pleine échelle), sans saturer
NORM_MAX_GAIN = 25.0       # amplification maximale
NORM_NOISE_FLOOR = 80      # en dessous : silence, on ne touche pas


def boost_quiet_audio(pcm: bytes) -> tuple[bytes, int]:
    """Normalise proprement le niveau du segment pour le moteur ASR.

    Mise à l'échelle linéaire unique du segment entier vers un niveau cible
    confortable, SANS saturation (gain = cible / crête), bien meilleure pour
    la reconnaissance qu'une amplification par à-coups. Renvoie (audio, crête).
    """
    peak = audioop.max(pcm, 2)
    if peak < NORM_NOISE_FLOOR:
        return pcm, peak
    gain = min(NORM_MAX_GAIN, NORM_TARGET_PEAK / peak)
    if gain <= 1.05:
        return pcm, peak
    try:
        return audioop.mul(pcm, 2, gain), peak
    except Exception:
        return pcm, peak


async def _post_transcription(data: dict, wav: bytes) -> httpx.Response:
    return await http.post(
        f"{ASR_BASE_URL}/audio/transcriptions",
        headers={"Authorization": f"Bearer {ASR_API_KEY}"},
        data=data,
        files={"file": ("segment.wav", wav, "audio/wav")},
    )


async def transcribe(pcm: bytes) -> str:
    wav = pcm_to_wav(pcm)
    data = {"model": ASR_MODEL}
    if ASR_LANGUAGE:
        data["language"] = ASR_LANGUAGE
    resp = await _post_transcription(data, wav)
    if resp.status_code == 400 and "language" in data:
        # Certains moteurs (ex. Qwen3-ASR via vLLM) rejettent le paramètre
        # language : on retente en laissant la détection automatique.
        print(f"ASR a refusé language={ASR_LANGUAGE!r} ({resp.text[:200]}), "
              "nouvel essai sans ce paramètre", flush=True)
        del data["language"]
        resp = await _post_transcription(data, wav)
    if resp.status_code != 200:
        raise RuntimeError(f"ASR HTTP {resp.status_code} : {resp.text[:300]}")
    return resp.json().get("text", "").strip()


# ---------------------------------------------------------------- WebSocket

@app.websocket("/ws")
async def ws_transcribe(ws: WebSocket):
    if not session_valid(ws.cookies.get(SESSION_COOKIE, "")):
        await ws.close(code=4401)
        return
    await ws.accept()

    # Premier message : {"type": "start", "title": "..."}
    try:
        start = json.loads(await ws.receive_text())
        assert start.get("type") == "start"
    except Exception:
        await ws.close(code=4000)
        return

    title = (start.get("title") or "").strip() or datetime.now().strftime("Réunion du %d/%m/%Y %H:%M")
    # Diarisation active si le serveur la supporte ET que le client la demande
    session_diarization = DIARIZATION and start.get("diarization", True)
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
        """Identifie le locuteur puis transcrit les segments, dans l'ordre."""
        # Chaque session a son propre diarizer (profils de locuteurs isolés)
        diarizer = None
        if session_diarization and embedding_model is not None:
            from diarizer import SpeakerDiarizer
            diarizer = SpeakerDiarizer(
                model=embedding_model,
                threshold=DIARIZATION_THRESHOLD,
                max_speakers=DIARIZATION_MAX_SPEAKERS,
            )

        while True:
            seg = await queue.get()
            if seg is None:
                return
            pcm, peak = boost_quiet_audio(seg.pcm)
            print(f"[réunion {meeting_id}] segment {seg.t0:.1f}s → {seg.t1:.1f}s "
                  f"({len(seg.pcm) / 2 / SAMPLE_RATE:.1f}s d'audio, niveau crête {peak}"
                  f"{', amplifié' if pcm is not seg.pcm else ''}), transcription...", flush=True)

            # Diarisation (~30-80 ms CPU). Strictement isolée : ni un blocage
            # ni une erreur ne doivent empêcher la transcription qui suit.
            speaker = ""
            if diarizer is not None:
                try:
                    speaker = await asyncio.wait_for(
                        asyncio.to_thread(diarizer.identify, pcm), timeout=5.0
                    )
                except Exception as exc:
                    print(f"[réunion {meeting_id}] diarisation ignorée : {exc}", flush=True)

            try:
                text = await transcribe(pcm)
            except Exception as exc:
                print(f"[réunion {meeting_id}] ERREUR ASR : {exc}", flush=True)
                await ws.send_json({"type": "error", "message": f"Transcription échouée : {exc}"})
                continue
            cleaned = clean_transcript(text)
            if cleaned != text:
                print(f"[réunion {meeting_id}] filtré (CJK) : {text[:60]!r} -> {cleaned[:60]!r}", flush=True)
            text = cleaned
            print(f"[réunion {meeting_id}] {speaker or 'texte'} : {text[:80]!r}", flush=True)
            if not text:
                continue
            await db.execute(
                "INSERT INTO segments (meeting_id, t0, t1, text, speaker) VALUES (?, ?, ?, ?, ?)",
                (meeting_id, seg.t0, seg.t1, text, speaker),
            )
            await db.commit()
            await ws.send_json(
                {"type": "segment", "t0": seg.t0, "t1": seg.t1, "text": text, "speaker": speaker}
            )

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
                cmd = control.get("type")
                if cmd == "stop":
                    if (last := segmenter.flush()) is not None:
                        queue.put_nowait(last)
                    queue.put_nowait(None)
                    await worker_task
                    worker_task = None
                    await ws.send_json({"type": "done", "meeting_id": meeting_id})
                    break
                elif cmd == "pause":
                    # clôt le segment en cours ; le client cesse d'envoyer l'audio
                    if (last := segmenter.flush()) is not None:
                        queue.put_nowait(last)
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
        "SELECT t0, t1, text, speaker FROM segments WHERE meeting_id = ? ORDER BY id",
        (meeting_id,),
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
        for s in segs:
            prefix = f"**{s['speaker']} —** " if s.get("speaker") else ""
            lines.append(f"**[{fmt_ts(s['t0'])}]** {prefix}{s['text']}")
        body, mime, ext = "\n\n".join(lines) + "\n", "text/markdown", "md"
    elif format == "srt":
        blocks = []
        for i, s in enumerate(segs, 1):
            speaker_line = f"<i>{s['speaker']}</i>\n" if s.get("speaker") else ""
            blocks.append(
                f"{i}\n{fmt_ts(s['t0'], srt=True)} --> {fmt_ts(s['t1'], srt=True)}\n"
                f"{speaker_line}{s['text']}"
            )
        body, mime, ext = "\n\n".join(blocks) + "\n", "application/x-subrip", "srt"
    elif format == "txt":
        def txt_line(s: dict) -> str:
            return f"[{s['speaker']}] {s['text']}" if s.get("speaker") else s["text"]
        body, mime, ext = "\n".join(txt_line(s) for s in segs) + "\n", "text/plain", "txt"
    else:
        raise HTTPException(400, "Format inconnu (txt, md, srt, json)")

    return Response(
        content=body,
        media_type=f"{mime}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}.{ext}"'},
    )


class NoCacheStaticFiles(StaticFiles):
    """Force la revalidation des fichiers statiques après chaque mise à jour."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", NoCacheStaticFiles(directory="static", html=True), name="static")
