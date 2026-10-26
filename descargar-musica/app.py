import hmac
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, Response, jsonify, render_template, request, send_file
from yt_dlp.utils import DownloadCancelled
from yt_dlp.version import __version__ as YTDLP_VERSION

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
WORK_DIR = tempfile.gettempdir()
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtu.be"}
QUALITIES = {"128", "192", "320"}
MAX_PLAYLIST = 50
SEARCH_RESULTS = 15
MAX_SEARCH = 60
PARALLEL_JOBS = 2
MAX_QUEUE = 20         # máximo de descargas esperando a la vez
# Para compartir la app: define ACCESS_PASSWORD (y HOST=0.0.0.0 si es por WiFi)
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")
# Carpeta de música: variable MUSIC_DIR o la carpeta "Music" de tu usuario
MUSIC_DIR = os.environ.get("MUSIC_DIR") or os.path.join(os.path.expanduser("~"), "Music")

# ffmpeg: variable de entorno > PATH del sistema > tu ruta anterior
FFMPEG = (
    os.environ.get("FFMPEG_LOCATION")
    or (os.path.dirname(shutil.which("ffmpeg")) if shutil.which("ffmpeg") else None)
    or r"C:\Users\YASUAN\Downloads\ffmpeg-2026-10-19-git-2344518cc5-essentials_build\bin"
)

app = Flask(__name__)
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=PARALLEL_JOBS)

NOISE = r"(?:official|oficial|video|vídeo|videoclip|lyrics?|letra|audio|visuali[sz]er|hd|4k|mv|clip)"


# ---------- acceso ----------
def is_local() -> bool:
    """True solo si la petición viene de tu propio computador (no de un túnel ni de la red)."""
    proxied = any(h in request.headers for h in ("X-Forwarded-For", "Cf-Connecting-Ip", "Forwarded"))
    return request.remote_addr in ("127.0.0.1", "::1") and not proxied


@app.before_request
def guard():
    if is_local():
        return None
    if not ACCESS_PASSWORD:
        return Response("Acceso no permitido: la app no está configurada para compartirse.", 403)
    auth = request.authorization
    if not (auth and hmac.compare_digest((auth.password or "").encode(), ACCESS_PASSWORD.encode())):
        time.sleep(1)  # frena los intentos de adivinar la contraseña
        return Response("Se necesita la contraseña.", 401,
                        {"WWW-Authenticate": 'Basic realm="Mi musica"'})
    return None


# ---------- utilidades ----------
def is_youtube_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and (p.hostname or "") in ALLOWED_HOSTS


def clean_error(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()
    low = msg.lower()
    if "sign in to confirm" in low or "cookies" in low:
        return "YouTube pidió verificación. Actualiza tu cookies.txt e inténtalo de nuevo."
    if "ffmpeg" in low or "ffprobe" in low:
        return "No se encontró ffmpeg. Revisa la ruta en FFMPEG_LOCATION."
    if "video unavailable" in low or "private video" in low:
        return "Ese video no está disponible o es privado."
    return msg[:300]


def clean_title(title: str, channel: str = "") -> tuple[str, str]:
    """Devuelve (artista, canción) sin ruidos como 'Video Letra' o '- Topic'."""
    channel = channel or ""
    official = bool(re.search(r"-\s*Topic$|VEVO$", channel, re.I))  # canal de artista
    channel = re.sub(r"\s*-\s*Topic$|VEVO$", "", channel, flags=re.I).strip()
    t = title.strip()
    if channel:  # quita " - NombreDelCanal" al final
        t = re.sub(rf"\s*[-–|]\s*{re.escape(channel)}\s*$", "", t, flags=re.I)
    t = re.sub(rf"\s*[\(\[][^\)\]]*\b{NOISE}\b[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(rf"\s*[-–|,]\s*{NOISE}(?:\s+{NOISE})*\s*$", "", t, flags=re.I)
    t = t.strip(" -–|,")
    if " - " in t:
        artist, song = (x.strip() for x in t.split(" - ", 1))
    else:
        artist, song = (channel if official else ""), t
    return artist, song or title.strip()


def base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "remote_components": ["ejs:github"],
        "ffmpeg_location": FFMPEG,
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def to_item(e: dict) -> dict:
    vid = e["id"]
    return {
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": e.get("title") or "Sin título",
        "channel": e.get("channel") or e.get("uploader") or "",
        "duration": e.get("duration"),
        "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
    }


def valid_entries(info: dict) -> list[dict]:
    return [to_item(e) for e in info.get("entries") or []
            if e and e.get("id") and not (e.get("title") or "").startswith("[")]


def safe_name(title: str) -> str:
    s = "".join(c for c in title if c.isalnum() or c in " -_()[]").strip()
    return s or "audio"


def unique_path(folder: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    return path


def cleanup(uid: str):
    for name in os.listdir(WORK_DIR):
        if name.startswith(uid + "."):
            try:
                os.remove(os.path.join(WORK_DIR, name))
            except OSError:
                pass


def purge_old():
    cutoff = time.time() - 3600
    with LOCK:
        for jid, j in list(JOBS.items()):
            if j["created"] < cutoff:
                cleanup(j["uid"])
                JOBS.pop(jid, None)


# ---------- trabajo de descarga ----------
def run_job(job: dict):
    uid = job["uid"]
    if job["cancel"]:
        job["status"] = "cancelled"
        return

    def check():
        if job["cancel"]:
            raise DownloadCancelled("cancelado")

    def on_download(d):
        check()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                job["percent"] = int(d["downloaded_bytes"] / total * 85)

    def on_post(d):
        check()
        if d["status"] == "started":
            job["status"] = "converting"
            job["percent"] = max(job["percent"], 90)

    def attempt(square_cover: bool):
        opts = base_opts() | {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(WORK_DIR, f"{uid}.%(ext)s"),
            "writethumbnail": True,
            "progress_hooks": [on_download],
            "postprocessor_hooks": [on_post],
            "postprocessors": [
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                 "preferredquality": job["quality"]},
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
        }
        if square_cover:  # recorta la portada al centro, en cuadrado
            opts["postprocessor_args"] = {"thumbnailsconvertor+ffmpeg_o":
                ["-c:v", "mjpeg", "-vf", "crop='min(iw,ih)':'min(iw,ih)'"]}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job["url"], download=False)
            artist, song = clean_title(info.get("title") or "",
                                       info.get("channel") or info.get("uploader") or "")
            info["title"] = song
            if artist:
                info["artist"] = artist
            job["name"] = f"{artist} - {song}" if artist else song
            ydl.process_ie_result(info, download=True)

    job["status"] = "downloading"
    try:
        try:
            attempt(True)
        except DownloadCancelled:
            raise
        except Exception as e:  # si falla el recorte de portada, reintenta sin recortar
            if not re.search(r"thumbnail|ffmpeg", str(e), re.I):
                raise
            cleanup(uid)
            job["percent"] = 0
            attempt(False)
        src = os.path.join(WORK_DIR, f"{uid}.mp3")
        if not os.path.exists(src):
            raise RuntimeError("No se pudo generar el MP3.")
        if job["save"]:  # guarda directo en la carpeta de música
            os.makedirs(MUSIC_DIR, exist_ok=True)
            dest = unique_path(MUSIC_DIR, safe_name(job["name"] or job["title"]) + ".mp3")
            shutil.move(src, dest)
            job["saved"] = os.path.basename(dest)
            cleanup(uid)
        job.update(status="done", percent=100)
    except DownloadCancelled:
        job["status"] = "cancelled"
        cleanup(uid)
    except Exception as e:
        job.update(status="error", error=clean_error(e))
        cleanup(uid)


# ---------- rutas ----------
@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/resolve")
def resolve():
    data = request.get_json(silent=True) or {}
    q = (data.get("q") or "").strip()
    try:
        count = max(1, min(int(data.get("count") or SEARCH_RESULTS), MAX_SEARCH))
    except (TypeError, ValueError):
        count = SEARCH_RESULTS
    if not q:
        return jsonify(error="Pega un link o escribe qué canción buscas."), 400
    try:
        if re.match(r"https?://", q, re.I):
            if not is_youtube_url(q):
                return jsonify(error="Ese link no parece ser de YouTube."), 400
            if urlparse(q).path.startswith("/playlist"):
                opts = base_opts() | {"noplaylist": False, "extract_flat": "in_playlist",
                                      "playlistend": MAX_PLAYLIST}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(q, download=False)
                kind, title, items = "playlist", info.get("title") or "Playlist", valid_entries(info)
            else:
                with yt_dlp.YoutubeDL(base_opts()) as ydl:
                    info = ydl.extract_info(q, download=False)
                kind, title, items = "video", info.get("title"), [to_item(info)]
        else:
            opts = base_opts() | {"extract_flat": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{count}:{q}", download=False)
            kind, title, items = "search", "Resultados", valid_entries(info)
    except Exception as e:
        return jsonify(error=clean_error(e)), 422
    if not items:
        return jsonify(error="No encontré resultados."), 404
    return jsonify(kind=kind, title=title, items=items)


@app.post("/api/jobs")
def create_job():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not is_youtube_url(url):
        return jsonify(error="Ese link no parece ser de YouTube."), 400
    quality = str(data.get("quality", "192"))
    purge_old()
    if sum(j["status"] in ("queued", "downloading", "converting") for j in JOBS.values()) >= MAX_QUEUE:
        return jsonify(error="Hay muchas descargas en cola. Intenta en unos minutos."), 429
    job = {"id": uuid.uuid4().hex, "uid": uuid.uuid4().hex[:8], "url": url,
           "quality": quality if quality in QUALITIES else "192",
           "title": str(data.get("title") or "audio")[:200], "name": None,
           "status": "queued", "percent": 0, "error": None, "cancel": False,
           "save": bool(data.get("save")) and is_local(), "saved": None,
           "created": time.time()}
    with LOCK:
        JOBS[job["id"]] = job
    POOL.submit(run_job, job)
    return jsonify(id=job["id"])


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify(error="La descarga ya no existe. Inténtalo de nuevo."), 404
    return jsonify(status=j["status"], percent=j["percent"], error=j["error"], name=j["name"],
                   saved=j["saved"])


@app.post("/api/jobs/<job_id>/cancel")
def job_cancel(job_id):
    j = JOBS.get(job_id)
    if j and j["status"] in ("queued", "downloading", "converting"):
        j["cancel"] = True
        if j["status"] == "queued":
            j["status"] = "cancelled"
    return jsonify(ok=True)


@app.get("/api/jobs/<job_id>/file")
def job_file(job_id):
    j = JOBS.get(job_id)
    if not j or j["status"] != "done":
        return jsonify(error="El archivo no está listo."), 404
    path = os.path.join(WORK_DIR, f"{j['uid']}.mp3")
    try:
        with open(path, "rb") as f:
            buf = io.BytesIO(f.read())
    except OSError:
        return jsonify(error="El archivo ya fue descargado."), 410
    cleanup(j["uid"])
    JOBS.pop(job_id, None)
    name = safe_name(j["name"] or j["title"])
    return send_file(buf, as_attachment=True, download_name=f"{name}.mp3", mimetype="audio/mpeg")


@app.get("/api/version")
def version():
    return jsonify(version=YTDLP_VERSION, local=is_local(),
                   music_dir=MUSIC_DIR if is_local() else "")


@app.post("/api/update")
def update():
    if not is_local() or request.headers.get("X-Requested-With") != "app":
        return jsonify(error="No permitido."), 403
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                           capture_output=True, text=True, timeout=240)
        if r.returncode:
            return jsonify(error="No se pudo actualizar yt-dlp."), 500
        new = subprocess.run(
            [sys.executable, "-c", "from yt_dlp.version import __version__ as v; print(v)"],
            capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500
    return jsonify(version=new, restart=bool(new) and new != YTDLP_VERSION)


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)