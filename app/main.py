import asyncio
import hashlib
import hmac
import io
import json
import os
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
ASR_LANGUAGE = os.environ.get("ASR_LANGUAGE", "").strip() or "fr"
DIARIZATION = os.environ.get("DIARIZATION", "off").strip().lower() == "on"
DIARIZATION_THRESHOLD = float(os.environ.get("DIARIZATION_THRESHOLD", "0.70"))
DIARIZATION_MAX_SPEAKERS = int(os.environ.get("DIARIZATION_MAX_SPEAKERS", "8"))

LIVEFLOW_USER = os.environ.get("LIVEFLOW_USER", "admin")
# Vide ou « admin » (ancien défaut public) : mot de passe aléatoire généré au
# premier démarrage, conservé dans le volume de données (voir load_password).
LIVEFLOW_PASSWORD = os.environ.get("LIVEFLOW_PASSWORD", "").strip()
SESSION_TTL = 7 * 24 * 3600  # 7 jours
SESSION_COOKIE = "liveflow_session"

db: aiosqlite.Connection | None = None
http: httpx.AsyncClient | None = None
embedding_model = None  # EmbeddingModel chargé au lifespan si DIARIZATION=True
session_secret: bytes = b""
login_password: str = ""


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


def load_password() -> str:
    """Mot de passe de connexion : LIVEFLOW_PASSWORD, sauf s'il est vide ou
    vaut l'ancien défaut public « admin » ; dans ce cas, mot de passe aléatoire
    généré une fois, conservé dans le volume et affiché dans les journaux."""
    if LIVEFLOW_PASSWORD and LIVEFLOW_PASSWORD != "admin":
        return LIVEFLOW_PASSWORD
    if LIVEFLOW_PASSWORD == "admin":
        print("ATTENTION : LIVEFLOW_PASSWORD=admin est refusé (mot de passe public).", flush=True)
    path = os.path.join(os.path.dirname(DB_PATH), "liveflow-password")
    try:
        with open(path, encoding="utf-8") as f:
            stored = f.read().strip()
        if stored:
            print(f"Mot de passe de connexion : voir {path} (ou définir LIVEFLOW_PASSWORD).", flush=True)
            return stored
    except FileNotFoundError:
        pass
    password = secrets.token_urlsafe(18)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(password + "\n")
    print("=" * 66, flush=True)
    print(f"Identifiants générés : {LIVEFLOW_USER} / {password}", flush=True)
    print(f"(affichés une seule fois, conservés dans {path} ;", flush=True)
    print(" définissez LIVEFLOW_PASSWORD pour choisir votre mot de passe)", flush=True)
    print("=" * 66, flush=True)
    return password


# Limitation des tentatives de connexion par IP : après MAX_LOGIN_FAILURES
# échecs, attente doublée à chaque nouvel échec (plafonnée à 15 min).
MAX_LOGIN_FAILURES = 5
MAX_LOCKOUT = 15 * 60
login_failures: dict[str, tuple[int, float]] = {}  # ip -> (échecs, bloqué jusqu'à)


def login_retry_after(ip: str) -> int:
    """Secondes d'attente avant une nouvelle tentative (0 : autorisée)."""
    _, locked_until = login_failures.get(ip, (0, 0.0))
    return max(0, int(locked_until - time.time() + 0.999))


def record_login_failure(ip: str) -> None:
    now = time.time()
    if len(login_failures) > 10_000:
        # Purge des entrées anciennes pour borner la mémoire
        for key, (_, until) in list(login_failures.items()):
            if until < now - 3600:
                del login_failures[key]
    failures = login_failures.get(ip, (0, 0.0))[0] + 1
    locked_until = 0.0
    if failures >= MAX_LOGIN_FAILURES:
        locked_until = now + min(MAX_LOCKOUT, 2 ** (failures - MAX_LOGIN_FAILURES + 1))
    login_failures[ip] = (failures, locked_until)


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
    global db, http, embedding_model, session_secret, login_password
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    session_secret = load_session_secret()
    login_password = load_password()
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
    # Migration : ajouter la colonne speaker si elle n'existe pas
    try:
        await db.execute("ALTER TABLE segments ADD COLUMN speaker TEXT NOT NULL DEFAULT ''")
        await db.commit()
    except Exception:
        pass  # la colonne existe déjà
    await db.execute("PRAGMA foreign_keys = ON")
    await db.commit()

    # Charger le modèle de diarisation (lourd, ~30s au premier lancement)
    if DIARIZATION:
        print("Chargement du modèle de diarisation ECAPA-TDNN…", flush=True)
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
async def login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "inconnue"
    retry_after = login_retry_after(ip)
    if retry_after:
        return JSONResponse(
            {"detail": f"Trop de tentatives, réessayez dans {retry_after} s"},
            status_code=429, headers={"Retry-After": str(retry_after)},
        )
    user_ok = hmac.compare_digest(body.username.encode(), LIVEFLOW_USER.encode())
    pass_ok = bool(login_password) and hmac.compare_digest(
        body.password.encode(), login_password.encode()
    )
    if not (user_ok and pass_ok):
        record_login_failure(ip)
        raise HTTPException(401, "Identifiants invalides")
    login_failures.pop(ip, None)
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

    # Premier message : {"type": "start", "title": "...", "diarization": bool}
    try:
        start = json.loads(await ws.receive_text())
        assert start.get("type") == "start"
    except Exception:
        await ws.close(code=4000)
        return

    title = (start.get("title") or "").strip() or datetime.now().strftime("Réunion du %d/%m/%Y %H:%M")
    # La diarisation est activée si le serveur la supporte ET le client la demande
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
        """Identifie le locuteur puis transcrit, dans l'ordre."""
        # Chaque session a son propre diarizer (profils locuteurs isolés)
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

            # Diarisation (~30-80 ms CPU, dans un thread pour ne pas bloquer)
            speaker = ""
            if diarizer is not None:
                try:
                    speaker = await asyncio.to_thread(diarizer.identify, seg.pcm)
                except Exception as exc:
                    print(f"Diarisation échouée : {exc}", flush=True)

            # Transcription ASR (~1-5 s réseau)
            try:
                text = await transcribe(seg.pcm)
            except Exception as exc:
                await ws.send_json({"type": "error", "message": f"Transcription échouée : {exc}"})
                continue
            if not text:
                continue

            await db.execute(
                "INSERT INTO segments (meeting_id, t0, t1, text, speaker) VALUES (?, ?, ?, ?, ?)",
                (meeting_id, seg.t0, seg.t1, text, speaker),
            )
            await db.commit()
            await ws.send_json({
                "type": "segment",
                "t0": seg.t0, "t1": seg.t1,
                "text": text, "speaker": speaker,
            })

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
                    # Mettre en pause : forcer la finalisation du segment audio en cours
                    if (last := segmenter.flush()) is not None:
                        queue.put_nowait(last)
                elif cmd == "resume":
                    pass
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
        def _txt_line(s: dict) -> str:
            return f"[{s['speaker']}] {s['text']}" if s.get("speaker") else s["text"]
        body, mime, ext = "\n".join(_txt_line(s) for s in segs) + "\n", "text/plain", "txt"
    else:
        raise HTTPException(400, "Format inconnu (txt, md, srt, json)")

    return Response(
        content=body,
        media_type=f"{mime}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}.{ext}"'},
    )


app.mount("/", StaticFiles(directory="static", html=True), name="static")
