"""Media Grabber - Instagram / YouTube downloader.

Dev:      python app.py            -> browser at http://127.0.0.1:5177
Window:   python app.py --window   -> native window (same as the packaged .exe)
Package:  powershell -File build.ps1
"""
import importlib.abc
import importlib.machinery
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

VERSION = "1.0.0"
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))          # bundled static files, icon, ffmpeg
# per-user, never shipped. MG_DATA_DIR points dev/test runs at a throwaway folder (and browser profile).
IS_WIN, IS_MAC = sys.platform == "win32", sys.platform == "darwin"
if IS_WIN:
    _default_data = Path(os.environ.get("APPDATA", Path.home())) / "MediaGrabber"
elif IS_MAC:
    _default_data = Path.home() / "Library" / "Application Support" / "MediaGrabber"
else:
    _default_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "MediaGrabber"
DATA_DIR = Path(os.environ.get("MG_DATA_DIR") or _default_data)
ENGINE_DIR = DATA_DIR / "engine"                            # updated yt-dlp / gallery-dl live here
for d in (DATA_DIR, ENGINE_DIR):
    d.mkdir(parents=True, exist_ok=True)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)     # no console flashes from ffmpeg / gallery-dl
ENGINE_PKGS = {"yt_dlp": "yt-dlp", "gallery_dl": "gallery-dl"}


class EngineFinder(importlib.abc.MetaPathFinder):
    """Load yt_dlp / gallery_dl from ENGINE_DIR when an update has been installed there.

    Sites like YouTube break the downloader every few weeks; this lets the packaged app
    take a newer engine from PyPI without rebuilding the .exe."""

    def find_spec(self, name, path, target=None):
        top = name.split(".")[0]
        if top not in ENGINE_PKGS or not (ENGINE_DIR / top / "__init__.py").exists():
            return None
        search = [str(ENGINE_DIR)] if "." not in name else path
        return importlib.machinery.PathFinder.find_spec(name, search)


sys.meta_path.insert(0, EngineFinder())


def fix_std_streams():
    """Windowed .exe children get None for stdout/stderr; reattach them to the pipes we were given."""
    if not IS_WIN or (sys.stdout is not None and sys.stderr is not None):
        return
    import ctypes
    import msvcrt
    for attr, std in (("stdout", -11), ("stderr", -12)):
        if getattr(sys, attr) is None:
            handle = ctypes.windll.kernel32.GetStdHandle(std)
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
                setattr(sys, attr, io.TextIOWrapper(os.fdopen(fd, "wb", 0), encoding="utf-8",
                                                    errors="replace", write_through=True))
            except OSError:
                setattr(sys, attr, open(os.devnull, "w"))


# The packaged app re-launches itself as the gallery-dl CLI (there is no separate python.exe to call).
if len(sys.argv) > 1 and sys.argv[1] == "--gallery-dl":
    fix_std_streams()
    import gallery_dl
    sys.argv = ["gallery-dl"] + sys.argv[2:]
    sys.exit(gallery_dl.main())

from flask import Flask, Response, jsonify, request, send_from_directory  # noqa: E402

import yt_dlp  # noqa: E402

# migrate a dev checkout's login/history into the per-user data folder
for _name in ("cookies.txt", "history.json"):
    if (APP_DIR / _name).exists() and not (DATA_DIR / _name).exists() and not FROZEN:
        shutil.copy2(APP_DIR / _name, DATA_DIR / _name)

import logging  # noqa: E402
from logging.handlers import RotatingFileHandler  # noqa: E402

log = logging.getLogger("mediagrabber")
log.setLevel(logging.INFO)
_handler = RotatingFileHandler(DATA_DIR / "media-grabber.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)
log.info("start version=%s frozen=%s data=%s", VERSION, FROZEN, DATA_DIR)

COOKIES_TXT = DATA_DIR / "cookies.txt"
HISTORY_JSON = DATA_DIR / "history.json"
SETTINGS_JSON = DATA_DIR / "settings.json"


def load_settings():
    try:
        return json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def set_out_dir(path):
    global OUT_DIR, TMP_DIR, THUMB_DIR
    OUT_DIR = Path(path)
    TMP_DIR = OUT_DIR / ".tmp"
    THUMB_DIR = OUT_DIR / ".thumbs"
    for d in (OUT_DIR, TMP_DIR, THUMB_DIR):
        d.mkdir(parents=True, exist_ok=True)


set_out_dir(load_settings().get("out_dir") or Path.home() / "Downloads" / "MediaGrabber")

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
PROXY_HOSTS = ("ytimg.com", "ggpht.com", "googleusercontent.com", "cdninstagram.com", "fbcdn.net")


def find_ffmpeg():
    bundled = RES_DIR / "bin" / ("ffmpeg.exe" if IS_WIN else "ffmpeg")
    if bundled.exists():
        return str(bundled.parent)
    if shutil.which("ffmpeg"):
        return os.path.dirname(shutil.which("ffmpeg"))
    import glob
    hits = glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe"))
    return os.path.dirname(hits[0]) if hits else None


FFMPEG_DIR = find_ffmpeg()
if FFMPEG_DIR:
    # child processes (gallery-dl -> its own yt-dlp) need ffmpeg on PATH to merge DASH video+audio
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")
FFMPEG = os.path.join(FFMPEG_DIR, "ffmpeg") if FFMPEG_DIR else "ffmpeg"

app = Flask(__name__, static_folder=str(RES_DIR / "static"), static_url_path="")
lock = threading.Lock()
slots = threading.Semaphore(3)   # max parallel downloads; the rest wait as "queued"
jobs = {}
probe_cache = {}


# ---------------------------------------------------------------- helpers
def platform_of(url):
    if "instagram.com" in url:
        return "Instagram"
    if "youtube.com" in url or "youtu.be" in url:
        return "YouTube"
    host = urllib.parse.urlparse(url).hostname or "Other"
    return host.replace("www.", "").split(".")[0].capitalize()


def is_ig_post(url):
    return re.search(r"instagram\.com/(?:[^/]+/)?p/", url) is not None


def clean_url(url):
    """Strip tracking junk (Postel: accept messy input, normalise it)."""
    url = url.strip().rstrip(".,;)]>'\"")
    p = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qsl(p.query)
    if "instagram.com" in p.netloc:
        q = []
    else:
        q = [(k, v) for k, v in q if k in ("v", "list", "t", "index")]
    return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q), fragment=""))


def extract_urls(text):
    return list(dict.fromkeys(clean_url(u) for u in re.findall(r"https?://[^\s<>\"']+", text or "")))


def kind_of(path):
    ext = Path(path).suffix.lower()
    return "video" if ext in VIDEO_EXT else "audio" if ext in AUDIO_EXT else "image" if ext in IMAGE_EXT else "file"


def cookie_args_ytdlp(opts):
    if COOKIES_TXT.exists():
        opts["cookiefile"] = str(COOKIES_TXT)
    return opts


def gdl_base():
    cmd = [sys.executable, "--gallery-dl"] if FROZEN else [sys.executable, str(Path(__file__).resolve()), "--gallery-dl"]
    if COOKIES_TXT.exists():
        cmd += ["--cookies", str(COOKIES_TXT)]
    return cmd


def proxied(u):
    return "/api/proxy?u=" + urllib.parse.quote(u, safe="") if u else None


def load_history():
    if not HISTORY_JSON.exists():
        return []
    try:
        return json.loads(HISTORY_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("could not read %s", HISTORY_JSON)
        return []


def save_history(items):
    tmp = HISTORY_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
    tmp.replace(HISTORY_JSON)


def friendly_error(msg, url):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(msg)).replace("ERROR: ", "").strip()
    if "instagram.com" in url and re.search(r"login|cookie|rate.?limit|401|403|empty media|redirect", msg, re.I):
        return ("Instagram only works when you are logged in." if not COOKIES_TXT.exists()
                else "Instagram refused the request. Your login may have expired, so log in again.")
    if re.search(r"not a bot", msg, re.I):
        return ("YouTube wants to check you are not a bot. Log in to YouTube, "
                "or wait an hour or two and try again.")
    if re.search(r"private video|sign in to confirm", msg, re.I):
        return "This video is private or age-restricted, so it needs a YouTube login (Settings)."
    if re.search(r"unsupported url", msg, re.I):
        return "That link isn't supported. Try a YouTube or Instagram link."
    return msg[:400]


# ---------------------------------------------------------------- probe (preview before download)
def probe_ytdlp(url):
    opts = cookie_args_ytdlp({"quiet": True, "no_warnings": True, "skip_download": True,
                              "noplaylist": "list=" not in url or "watch?v=" in url,
                              "extract_flat": "in_playlist"})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        return {
            "kind": "playlist", "title": info.get("title") or "Playlist",
            "uploader": info.get("uploader") or info.get("channel"),
            "count": len(entries),
            "thumbnail": proxied((entries[0].get("thumbnails") or [{}])[-1].get("url")) if entries else None,
            "entries": [{"title": e.get("title"), "duration": e.get("duration")} for e in entries[:8]],
        }

    formats = info.get("formats") or []
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max((f.get("filesize") or f.get("filesize_approx") or 0 for f in audio), default=0)
    heights = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        heights[h] = max(heights.get(h, 0), size)
    qualities = [{"height": h, "size": (s + best_audio) if s else None}
                 for h, s in sorted(heights.items(), reverse=True)]
    # collapse near-duplicate odd heights (e.g. 1918 vs 1920) and keep the list short (Hick's law)
    seen, slim = set(), []
    for q in qualities:
        label = min((2160, 1440, 1080, 720, 480, 360, 240, 144), key=lambda x: abs(x - q["height"]))
        if label not in seen:
            seen.add(label)
            slim.append({**q, "label": label})
    w, h = info.get("width"), info.get("height")
    return {
        "kind": "video", "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": proxied(info.get("thumbnail")),
        "vertical": bool(w and h and h > w),
        "qualities": slim[:6],
        "audio_size": best_audio or None,
    }


def probe_gallery(url):
    proc = subprocess.run(gdl_base() + ["-j", url], capture_output=True, timeout=90,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"}, creationflags=NO_WINDOW)
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
    except ValueError:
        data = []
    items, meta = [], {}
    for m in data:
        if m and m[0] == -1:
            raise RuntimeError(m[1].get("message") if isinstance(m[1], dict) else m[1])
        if m and m[0] == 3:
            k = m[2]
            meta = k
            is_video = bool(k.get("video_url")) or k.get("extension") == "mp4"
            items.append({"num": k.get("num"), "type": "video" if is_video else "image",
                          "thumb": proxied(k.get("display_url") or (m[1] if not is_video else None)),
                          "vertical": (k.get("height") or 0) > (k.get("width") or 0)})
    if not items:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1]
                           if proc.stderr.strip() else "No media found in that post")
    desc = (meta.get("description") or "").strip().split("\n")[0]
    return {
        "kind": "post", "title": desc[:120] or f"Post by @{meta.get('username')}",
        "uploader": "@" + meta["username"] if meta.get("username") else None,
        "thumbnail": items[0]["thumb"], "items": items,
        "has_video": any(i["type"] == "video" for i in items),
    }


@app.post("/api/probe")
def probe():
    url = clean_url(request.get_json(force=True).get("url", ""))
    cached = probe_cache.get(url)
    if cached and time.time() - cached[0] < 600:
        return jsonify(cached[1])
    try:
        info = probe_gallery(url) if is_ig_post(url) else probe_ytdlp(url)
    except Exception as e:  # noqa: BLE001 - surface any extractor error to the UI
        if "instagram.com" in url and not is_ig_post(url):
            try:
                info = probe_gallery(url)
            except Exception:  # noqa: BLE001
                return jsonify(error=friendly_error(e, url)), 422
        else:
            return jsonify(error=friendly_error(e, url)), 422
    info.update(url=url, platform=platform_of(url))
    probe_cache[url] = (time.time(), info)
    return jsonify(info)


# ---------------------------------------------------------------- download
class Cancelled(Exception):
    pass


def update(job_id, **kw):
    with lock:
        jobs[job_id].update(kw)


def run_ytdlp(job, stage_dir):
    sizes = {}

    def hook(d):
        if job.get("cancel"):
            raise Cancelled()
        if d["status"] == "downloading":
            sizes[d.get("filename")] = (d.get("downloaded_bytes") or 0,
                                        d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
            done = sum(a for a, _ in sizes.values())
            total = sum(b for _, b in sizes.values())
            track = "audio" if (d.get("info_dict") or {}).get("vcodec") == "none" and job["mode"] != "audio" else None
            update(job["id"], status="downloading", progress=(done / total * 100) if total else None,
                   speed=d.get("speed"), eta=d.get("eta"), stage=f"Downloading {track} track" if track else "Downloading")
        elif d["status"] == "finished":
            update(job["id"], stage="Processing")

    def pp_hook(d):
        if d["status"] == "started":
            names = {"Merger": "Merging video + audio", "ExtractAudio": "Converting audio",
                     "FFmpegVideoRemuxer": "Converting"}
            update(job["id"], status="processing", stage=names.get(d.get("postprocessor"), "Finishing up"),
                   progress=None)

    q = job["quality"]
    h = "" if q in ("best", None, "") else f"[height<={int(q) + 20}]"
    # resolution first, then the most compatible codec at that resolution (H.264 exists up to 1080p on YouTube)
    sort = ([f"res:{int(q)}"] if q not in ("best", None, "") else ["res"]) + ["vcodec:h264", "acodec:aac", "ext:mp4:m4a"]
    opts = cookie_args_ytdlp({
        "outtmpl": str(stage_dir / "%(title).90B [%(id)s].%(ext)s"),
        "progress_hooks": [hook], "postprocessor_hooks": [pp_hook],
        "noplaylist": not job.get("playlist"), "windowsfilenames": True,
        "quiet": True, "no_warnings": True, "noprogress": True,
        "ffmpeg_location": FFMPEG_DIR,
    })
    if job["mode"] == "audio":
        fmt = job.get("audio_format", "mp3")
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best" if fmt == "m4a" else "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": fmt,
                                   "preferredquality": "320" if fmt == "mp3" else "0"}]
    elif job["mode"] == "mute":
        opts["format"] = f"bestvideo{h}/best{h}"
        opts["format_sort"] = sort
        opts["postprocessor_args"] = {"ffmpeg": ["-an"]}
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    else:
        opts["format"] = f"bestvideo{h}+bestaudio/best{h}/best"
        opts["format_sort"] = sort
        opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(job["url"], download=True)
    return info


def run_gallery(job, stage_dir):
    cmd = gdl_base() + ["-D", str(stage_dir), "-f", "{username}_{post_shortcode}_{num}.{extension}",
]
    if job.get("items"):
        nums = ",".join(str(int(n)) for n in job["items"])
        cmd += ["--filter", f"num in ({nums},)"]
    cmd.append(job["url"])
    expected = len(job.get("items") or []) or job.get("count") or 1
    update(job["id"], status="downloading", stage="Downloading", progress=0)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, creationflags=NO_WINDOW)
    job["_proc"] = proc
    done = 0
    for line in proc.stdout:
        if job.get("cancel"):
            proc.kill()
            raise Cancelled()
        if line.strip():
            done += 1
            update(job["id"], progress=min(done / expected * 100, 99))
    proc.wait()
    if job.get("cancel"):
        raise Cancelled()
    if not any(stage_dir.iterdir()):
        err = proc.stderr.read().decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(err[-1] if err else "No media found")
    merge_dash_pairs(stage_dir)
    if job["mode"] in ("audio", "mute"):
        update(job["id"], status="processing", stage="Converting", progress=None)
        for p in list(stage_dir.iterdir()):
            if p.suffix.lower() in VIDEO_EXT:
                convert(p, job["mode"], job.get("audio_format", "mp3"))
    return {"title": job.get("title"), "thumbnail": None}


def merge_dash_pairs(folder):
    """Safety net: join separate DASH video/audio files (name.fdash-XXXv.mp4 + name.fdash-YYYa.m4a)."""
    groups = {}
    for p in folder.iterdir():
        m = re.match(r"(.+?)\.fdash-\w+?([va])\.(mp4|m4a)$", p.name)
        if m:
            groups.setdefault(m.group(1), {})[m.group(2)] = p
    for base, pair in groups.items():
        if "v" in pair and "a" in pair:
            out = folder / f"{base}.mp4"
            r = subprocess.run([FFMPEG, "-y", "-i", str(pair["v"]), "-i", str(pair["a"]),
                                "-c", "copy", str(out)], capture_output=True, creationflags=NO_WINDOW)
            if r.returncode == 0 and out.exists():
                pair["v"].unlink()
                pair["a"].unlink()
        elif "v" in pair:
            pair["v"].rename(folder / f"{base}.mp4")


def video_codec(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, creationflags=NO_WINDOW)
    m = re.search(r"Video: (\w+)", r.stderr.decode("utf-8", "replace"))
    return m.group(1) if m else None


def media_duration(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, creationflags=NO_WINDOW)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr.decode("utf-8", "replace"))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None


def ensure_h264(job, folder):
    """Instagram's full-quality stream is VP9, which WhatsApp, PowerPoint and many editors can't open.
    Re-encode those to H.264 at the same resolution (seconds for a reel)."""
    if not load_settings().get("compat_convert", True):
        return
    for p in list(folder.rglob("*")):
        if p.suffix.lower() not in VIDEO_EXT or video_codec(p) in (None, "h264"):
            continue
        duration = media_duration(p)
        update(job["id"], status="processing", stage="Converting so it plays everywhere", progress=0 if duration else None,
               eta=None, speed=None)
        out = p.with_name(p.stem + ".h264.mp4")
        proc = subprocess.Popen([FFMPEG, "-y", "-i", str(p), "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                                 "-progress", "pipe:1", "-nostats", str(out)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        job["_proc"] = proc
        started = time.time()
        for line in proc.stdout:   # ffmpeg -progress prints key=value lines, e.g. out_time_us=12345678
            if job.get("cancel"):
                proc.kill()
                raise Cancelled()
            key, _, val = line.decode("ascii", "replace").strip().partition("=")
            if key == "out_time_us" and duration and val.isdigit():
                done = min(int(val) / 1e6 / duration, 0.99)
                elapsed = time.time() - started
                eta = int(elapsed / done - elapsed) if done > 0.03 else None
                update(job["id"], progress=done * 100, eta=eta)
        proc.wait()
        job.pop("_proc", None)
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            p.unlink()
            out.rename(p.with_suffix(".mp4"))
        else:
            out.unlink(missing_ok=True)   # keep the original rather than nothing


def convert(path, mode, fmt):
    if mode == "mute":
        out = path.with_name(path.stem + "_muted.mp4")
        args = [FFMPEG, "-y", "-i", str(path), "-an", "-c:v", "copy", str(out)]
    else:
        out = path.with_suffix("." + fmt)
        args = [FFMPEG, "-y", "-i", str(path), "-vn"] + (
            ["-b:a", "320k"] if fmt == "mp3" else ["-c:a", "aac", "-b:a", "256k"]) + [str(out)]
    subprocess.run(args, capture_output=True, creationflags=NO_WINDOW)
    if out.exists():
        path.unlink()


def save_thumb(job_id, remote_url, files):
    dest = THUMB_DIR / f"{job_id}.jpg"
    if remote_url:
        try:
            raw = remote_url
            if raw.startswith("/api/proxy?u="):
                raw = urllib.parse.unquote(raw.split("u=", 1)[1])
            req = urllib.request.Request(raw, headers={"User-Agent": "Mozilla/5.0"})
            dest.write_bytes(urllib.request.urlopen(req, timeout=15).read())
            return f".thumbs/{dest.name}"
        except Exception:  # noqa: BLE001 - fall back to a local thumbnail
            pass
    for f in files:
        p = OUT_DIR / f["path"]
        if f["kind"] == "image":
            return f["path"]
        if f["kind"] == "video":
            subprocess.run([FFMPEG, "-y", "-ss", "1", "-i", str(p), "-frames:v", "1",
                            "-vf", "scale=480:-2", str(dest)], capture_output=True, creationflags=NO_WINDOW)
            if dest.exists():
                return f".thumbs/{dest.name}"
    return None


def move_into_library(stage_dir, platform):
    final_dir = OUT_DIR / platform
    final_dir.mkdir(exist_ok=True)
    files = []
    for p in sorted(stage_dir.rglob("*")):
        if not p.is_file() or p.suffix in (".part", ".ytdl", ".json") or p.name.endswith(".temp"):
            continue
        target = final_dir / p.name
        n = 2
        while target.exists():
            target = final_dir / f"{p.stem} ({n}){p.suffix}"
            n += 1
        shutil.move(str(p), target)
        files.append({"path": str(target.relative_to(OUT_DIR)).replace("\\", "/"),
                      "kind": kind_of(target), "size": target.stat().st_size})
    shutil.rmtree(stage_dir, ignore_errors=True)
    return files


def worker(job):
    update(job["id"], stage="Waiting for a free slot")
    with slots:
        if job.get("cancel"):
            update(job["id"], status="cancelled", stage="Cancelled")
            return
        update(job["id"], stage="Starting")
        run_job(job)


def run_job(job):
    stage_dir = TMP_DIR / job["id"]
    stage_dir.mkdir(parents=True, exist_ok=True)
    try:
        info = None
        if is_ig_post(job["url"]) or job.get("kind") == "post":
            run_gallery(job, stage_dir)
        else:
            try:
                info = run_ytdlp(job, stage_dir)
            except Cancelled:
                raise
            except Exception:
                if "instagram.com" in job["url"] and not any(stage_dir.iterdir()):
                    run_gallery(job, stage_dir)  # photo-only IG content has no video formats
                else:
                    raise
        if job["platform"] == "Instagram" and job["mode"] in ("video", "mute"):
            ensure_h264(job, stage_dir)
        files = move_into_library(stage_dir, job["platform"])
        if not files:
            raise RuntimeError("Nothing was downloaded")
        thumb = save_thumb(job["id"], job.get("thumbnail") or (info or {}).get("thumbnail"), files)
        entry = {
            "id": job["id"], "url": job["url"], "platform": job["platform"], "mode": job["mode"],
            "title": job.get("title") or (info or {}).get("title") or files[0]["path"].split("/")[-1],
            "uploader": job.get("uploader") or (info or {}).get("uploader"),
            "duration": job.get("duration") or (info or {}).get("duration"),
            "thumb": thumb, "files": files, "created": time.time(), "root": str(OUT_DIR),
        }
        with lock:
            hist = load_history()
            hist.insert(0, entry)
            save_history(hist)
        update(job["id"], status="done", progress=100, stage="Done", files=files, thumb=thumb, entry=entry)
    except Cancelled:
        shutil.rmtree(stage_dir, ignore_errors=True)
        update(job["id"], status="cancelled", stage="Cancelled")
    except Exception as e:  # noqa: BLE001
        log.exception("download failed: %s", job["url"])
        shutil.rmtree(stage_dir, ignore_errors=True)
        update(job["id"], status="error", stage="Failed", error=friendly_error(e, job["url"]))


@app.post("/api/download")
def start_download():
    body = request.get_json(force=True)
    url = clean_url(body.get("url", ""))
    if not url.startswith("http"):
        return jsonify(error="That isn't a link"), 400
    preview = (probe_cache.get(url) or (0, {}))[1]
    job = {
        "id": uuid.uuid4().hex[:10], "url": url, "platform": platform_of(url),
        "mode": body.get("mode", "video"), "quality": str(body.get("quality") or "best"),
        "audio_format": body.get("audio_format", "mp3"), "playlist": bool(body.get("playlist")),
        "items": body.get("items") or [], "count": len(preview.get("items") or []),
        "batch": body.get("batch"),
        "kind": preview.get("kind"),
        "title": preview.get("title"), "uploader": preview.get("uploader"),
        "duration": preview.get("duration"), "thumbnail": preview.get("thumbnail"),
        "status": "queued", "stage": "Starting", "progress": None, "created": time.time(),
    }
    with lock:
        jobs[job["id"]] = job
    threading.Thread(target=worker, args=(job,), daemon=True).start()
    return jsonify(id=job["id"])


def public(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


@app.get("/api/jobs")
def list_jobs():
    with lock:
        return jsonify(sorted((public(j) for j in jobs.values()), key=lambda j: -j["created"]))


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    with lock:
        job = jobs.get(job_id)
        if job:
            job["cancel"] = True
            if job.get("_proc"):
                job["_proc"].kill()
    return jsonify(ok=bool(job))


@app.post("/api/batches/<batch_id>/cancel")
def cancel_batch(batch_id):
    n = 0
    with lock:
        for job in jobs.values():
            if job.get("batch") == batch_id and job["status"] in ("queued", "downloading", "processing"):
                job["cancel"] = True
                if job.get("_proc"):
                    job["_proc"].kill()
                n += 1
    return jsonify(cancelled=n)


@app.post("/api/jobs/clear")
def clear_jobs():
    with lock:
        for k in [k for k, j in jobs.items() if j["status"] in ("done", "error", "cancelled")]:
            del jobs[k]
    return jsonify(ok=True)


# ---------------------------------------------------------------- library
DEFAULT_ROOT = Path.home() / "Downloads" / "MediaGrabber"


def root_of(item):
    """Library entries remember the folder they were saved into, so changing folders doesn't orphan them."""
    return Path(item.get("root") or DEFAULT_ROOT)


def known_roots():
    roots = [OUT_DIR] + [root_of(i) for i in load_history()]
    return list(dict.fromkeys(r.resolve() for r in roots))


def resolve_rel(rel):
    """Map a library-relative path to a real file, only inside folders this app saved into."""
    for root in known_roots():
        p = (root / rel).resolve()
        if root in p.parents and p.exists():
            return root, p
    raise ValueError("not found")


@app.get("/api/library")
def library():
    items = load_history()
    for it in items:
        it["missing"] = not any((root_of(it) / f["path"]).exists() for f in it["files"])
    return jsonify(items)


@app.delete("/api/library/<item_id>")
def delete_item(item_id):
    delete_files = request.args.get("files") == "1"
    with lock:
        hist = load_history()
        item = next((i for i in hist if i["id"] == item_id), None)
        if not item:
            return jsonify(error="not found"), 404
        if delete_files:
            root = root_of(item)
            for f in item["files"]:
                (root / f["path"]).unlink(missing_ok=True)
            if (item.get("thumb") or "").startswith(".thumbs/"):
                (root / item["thumb"]).unlink(missing_ok=True)
        save_history([i for i in hist if i["id"] != item_id])
    return jsonify(ok=True)


@app.post("/api/reveal")
def reveal():
    body = request.get_json(force=True)
    rel = body.get("path") or "."
    if rel == ".":
        p = OUT_DIR
    else:
        try:
            _, p = resolve_rel(rel)
        except ValueError:
            return jsonify(error="File not found"), 404
    open_in_os(p, reveal=p.is_file() and not body.get("open"))
    return jsonify(ok=True)


def open_in_os(p, reveal=False):
    """Open a file/folder in its default app, or show a file selected in Explorer / Finder."""
    if IS_WIN:
        if reveal:
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(p)
    elif IS_MAC:
        subprocess.Popen(["open", "-R", str(p)] if reveal else ["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if reveal else p)])


@app.get("/files/<path:rel>")
def get_file(rel):
    try:
        root, _ = resolve_rel(rel)
    except ValueError:
        return "", 404
    return send_from_directory(root, rel, as_attachment=request.args.get("dl") == "1")


@app.get("/api/proxy")
def proxy():
    """Instagram's CDN blocks cross-site embedding, so previews are fetched server-side."""
    u = request.args.get("u", "")
    host = urllib.parse.urlparse(u).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in PROXY_HOSTS):
        return "", 403
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=15)
        return Response(r.read(), mimetype=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "max-age=86400"})
    except Exception:  # noqa: BLE001
        return "", 502


# ---------------------------------------------------------------- settings
@app.post("/api/cookies")
def save_cookies():
    """Accept a Netscape cookies.txt export, or just a bare Instagram sessionid value."""
    text = (request.get_json(force=True).get("text") or "").strip().replace("\r\n", "\n")
    if not text:
        return jsonify(error="Nothing pasted"), 400
    if "\t" in text:
        new_lines = [ln for ln in text.split("\n") if ln.count("\t") >= 6 and not ln.startswith("# ")]
    else:
        value = text.split("sessionid=", 1)[-1].split(";")[0].strip().strip('"')
        if not re.fullmatch(r"[\w%:.\-]{20,}", value):
            return jsonify(error="That doesn't look like a sessionid or a cookies.txt file"), 400
        expiry = int(time.time()) + 365 * 86400
        new_lines = [f".instagram.com\tTRUE\t/\tTRUE\t{expiry}\tsessionid\t{value}"]
    if not new_lines:
        return jsonify(error="No cookies found in that text"), 400
    # a pasted export replaces that site's login; a bare sessionid only replaces that one cookie
    sites = store_cookie_lines(new_lines, replace_site="\t" in text)
    return jsonify(ok=True, sites=sorted(s for s in sites if s))


def store_cookie_lines(new_lines, replace_site=True):
    """Merge by site, so adding a YouTube login keeps the Instagram one (and vice versa)."""
    sites = {site_of(ln) for ln in new_lines}
    new_keys = {(ln.split("\t")[0], ln.split("\t")[5]) for ln in new_lines}
    with lock:
        kept = [ln for ln in read_cookie_lines()
                if (ln.split("\t")[0], ln.split("\t")[5]) not in new_keys
                and (not replace_site or site_of(ln) not in sites)]
        write_cookie_lines(kept + new_lines)
    probe_cache.clear()
    return sites


def site_of(line):
    domain = line.split("\t", 1)[0].lstrip(".").replace("#HttpOnly_", "")
    for s in ("instagram", "youtube", "google"):
        if s in domain:
            return "youtube" if s == "google" else s
    return domain


def read_cookie_lines():
    if not COOKIES_TXT.exists():
        return []
    return [ln for ln in COOKIES_TXT.read_text(encoding="utf-8", errors="ignore").split("\n")
            if ln.count("\t") >= 6 and not ln.startswith("# ")]


def write_cookie_lines(lines):
    if lines:
        COOKIES_TXT.write_text("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n", encoding="utf-8")
    else:
        COOKIES_TXT.unlink(missing_ok=True)


@app.delete("/api/cookies")
def delete_cookies():
    site = request.args.get("site")
    write_cookie_lines([ln for ln in read_cookie_lines() if site and site_of(ln) != site])
    probe_cache.clear()
    return jsonify(ok=True)


@app.post("/api/extract")
def extract():
    return jsonify(urls=extract_urls(request.get_json(force=True).get("text", "")))


@app.get("/api/info")
def info():
    # only count real auth cookies; yt-dlp itself writes harmless PREF/SOCS cookies into the jar
    names = {(site_of(ln), ln.split("\t")[5]) for ln in read_cookie_lines()}
    ig = ("instagram", "sessionid") in names
    yt = any(s == "youtube" and n in ("SAPISID", "__Secure-3PSID", "LOGIN_INFO", "SID") for s, n in names)
    return jsonify(out_dir=str(OUT_DIR), ffmpeg=bool(FFMPEG_DIR), version=VERSION,
                   engines=engine_versions(), update=UPDATE_STATE, window=WINDOW is not None,
                   instagram_login=ig, youtube_login=yt, remote=not is_local(), host=socket.gethostname(),
                   compat_convert=load_settings().get("compat_convert", True),
                   sharing=NETWORK["server"] is not None)


@app.post("/api/settings")
def save_settings():
    body = request.get_json(force=True)
    settings = load_settings()
    if "compat_convert" in body:
        settings["compat_convert"] = bool(body["compat_convert"])
    if body.get("out_dir"):
        target = Path(body["out_dir"]).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
            (target / ".write-test").write_text("ok")
            (target / ".write-test").unlink()
        except OSError:
            return jsonify(error="Can't save files in that folder"), 400
        settings["out_dir"] = str(target)
        set_out_dir(target)
    SETTINGS_JSON.write_text(json.dumps(settings, indent=1), encoding="utf-8")
    return jsonify(ok=True, out_dir=str(OUT_DIR))


# ---------------------------------------------------------------- engine updates (yt-dlp / gallery-dl from PyPI)
UPDATE_STATE = {"checked": False, "available": {}, "error": None}


def engine_versions():
    import gallery_dl.version
    return {"yt-dlp": yt_dlp.version.__version__, "gallery-dl": gallery_dl.version.__version__}


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def pypi_wheel(name):
    data = json.loads(urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=20).read())
    wheel = next(u for u in data["urls"] if u["packagetype"] == "bdist_wheel")
    return data["info"]["version"], wheel["url"], wheel["digests"]["sha256"]


def check_updates():
    try:
        current = engine_versions()
        avail = {}
        for name in ENGINE_PKGS.values():
            latest, _, _ = pypi_wheel(name)
            if vtuple(latest) > vtuple(current[name]):
                avail[name] = latest
        UPDATE_STATE.update(checked=True, available=avail, error=None)
    except Exception as e:  # noqa: BLE001 - offline etc.
        UPDATE_STATE.update(checked=True, error=str(e)[:200])
    return UPDATE_STATE


@app.post("/api/update/check")
def update_check():
    return jsonify(check_updates())


@app.post("/api/update/apply")
def update_apply():
    import hashlib
    import zipfile
    installed = {}
    for mod, name in ENGINE_PKGS.items():
        if name not in UPDATE_STATE["available"]:
            continue
        version, url, sha = pypi_wheel(name)
        raw = urllib.request.urlopen(url, timeout=120).read()
        if hashlib.sha256(raw).hexdigest() != sha:
            return jsonify(error=f"{name} download was corrupted; try again"), 502
        staging = ENGINE_DIR / f".new-{mod}"
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for member in z.namelist():
                if member.startswith(mod + "/") and ".." not in member:
                    z.extract(member, staging)
        shutil.rmtree(ENGINE_DIR / mod, ignore_errors=True)
        (staging / mod).rename(ENGINE_DIR / mod)
        shutil.rmtree(staging, ignore_errors=True)
        installed[name] = version
    UPDATE_STATE["available"] = {k: v for k, v in UPDATE_STATE["available"].items() if k not in installed}
    return jsonify(ok=True, installed=installed, restart=bool(installed))


@app.post("/api/restart")
def restart():
    INSTANCE_JSON.unlink(missing_ok=True)
    args = [sys.executable] + ([] if FROZEN else [str(Path(__file__).resolve())]) + ["--window"]
    subprocess.Popen(args, cwd=str(APP_DIR), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    threading.Timer(0.6, lambda: os._exit(0)).start()
    return jsonify(ok=True)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------- office network mode
# The host PC can share the app on the local network: other devices (Mac, iPhone, Android, PCs) open
# http://<host>:<port> in a browser and enter a 6-digit PIN. Remote users can preview, download and
# save files to their own device; anything that touches the host machine itself stays host-only.
import hashlib  # noqa: E402
import hmac  # noqa: E402
import secrets  # noqa: E402
from datetime import timedelta  # noqa: E402

from flask import redirect, session  # noqa: E402

_secret_file = DATA_DIR / "secret.key"
if not _secret_file.exists():
    _secret_file.write_bytes(secrets.token_bytes(32))
app.secret_key = _secret_file.read_bytes()
app.config.update(SESSION_COOKIE_NAME="mg_session", SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))

NETWORK = {"server": None, "port": None, "error": None}
PIN_ATTEMPTS = {}   # ip -> [timestamps]
HOST_ONLY = {"reveal", "save_settings", "save_cookies", "delete_cookies", "start_login", "update_check",
             "update_apply", "restart", "focus", "delete_item", "network_set", "network_new_pin", "network_status"}
REMOTE_OPEN = {"pin_page", "pin_submit", "static"}


def is_local():
    if os.environ.get("MG_REMOTE_TEST") and NETWORK["port"] and request.host.endswith(f":{NETWORK['port']}"):
        return False   # dev-only: lets the remote UI be tested from this PC through the network port
    return request.remote_addr in ("127.0.0.1", "::1")


def pin_tag(pin):
    return hashlib.sha256(f"mg:{pin}".encode()).hexdigest()[:16]


@app.before_request
def guard_remote():
    if is_local():
        return None
    if NETWORK["server"] is None:
        return jsonify(error="Sharing is off"), 403
    if request.endpoint in REMOTE_OPEN:
        return None
    pin = load_settings().get("network_pin", "")
    if not pin or session.get("pin") != pin_tag(pin):
        if request.path.startswith("/api/"):
            return jsonify(error="Enter the PIN first"), 401
        return redirect("/pin")
    if request.endpoint in HOST_ONLY:
        return jsonify(error="Only the host PC can do that"), 403
    return None


@app.get("/pin")
def pin_page():
    return send_from_directory(app.static_folder, "pin.html")


@app.post("/api/pin")
def pin_submit():
    ip = request.remote_addr or "?"
    now = time.time()
    recent = [t for t in PIN_ATTEMPTS.get(ip, []) if now - t < 600]
    if len(recent) >= 8:
        return jsonify(error="Too many tries. Wait 10 minutes and try again."), 429
    pin = load_settings().get("network_pin", "")
    given = re.sub(r"\D", "", str(request.get_json(force=True).get("pin", "")))
    if pin and hmac.compare_digest(given, pin):
        PIN_ATTEMPTS.pop(ip, None)
        session.permanent = True
        session["pin"] = pin_tag(pin)
        log.info("network: %s signed in", ip)
        return jsonify(ok=True)
    PIN_ATTEMPTS[ip] = recent + [now]
    return jsonify(error="That PIN isn't right"), 403


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # no packet is sent; just picks the LAN interface
            return s.getsockname()[0]
    except OSError:
        return None


def start_network(port):
    from werkzeug.serving import make_server
    stop_network()
    try:
        server = make_server("0.0.0.0", port, app, threaded=True)
    except OSError as e:
        NETWORK.update(error=f"Port {port} is already in use ({e.strerror})")
        return False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    NETWORK.update(server=server, port=port, error=None)
    log.info("network: sharing on port %s", port)
    return True


def stop_network():
    if NETWORK["server"] is not None:
        NETWORK["server"].shutdown()
        NETWORK.update(server=None, port=None)
        log.info("network: sharing stopped")


def network_payload():
    st = load_settings()
    port = NETWORK["port"] or st.get("network_port", 5180)
    host = socket.gethostname()
    ip = lan_ip()
    urls = [u for u in (f"http://{ip}:{port}" if ip else None, f"http://{host}:{port}") if u]
    qr = None
    if NETWORK["server"] is not None and urls:
        try:
            import qrcode
            import qrcode.image.svg
            img = qrcode.make(urls[0], image_factory=qrcode.image.svg.SvgPathImage, border=2)
            qr = img.to_string(encoding="unicode")
        except Exception:  # noqa: BLE001
            log.exception("qr failed")
    return {"enabled": NETWORK["server"] is not None, "port": port, "pin": st.get("network_pin"),
            "urls": urls, "qr": qr, "error": NETWORK["error"], "host": host}


@app.get("/api/network")
def network_status():
    return jsonify(network_payload())


@app.post("/api/network")
def network_set():
    st = load_settings()
    enabled = bool(request.get_json(force=True).get("enabled"))
    if enabled:
        st.setdefault("network_pin", f"{secrets.randbelow(10**6):06d}")
        ok = start_network(int(st.get("network_port", 5180)))
        st["network_enabled"] = ok
    else:
        stop_network()
        st["network_enabled"] = False
    SETTINGS_JSON.write_text(json.dumps(st, indent=1), encoding="utf-8")
    return jsonify(network_payload())


@app.post("/api/network/pin")
def network_new_pin():
    st = load_settings()
    st["network_pin"] = f"{secrets.randbelow(10**6):06d}"   # signs everyone else out
    SETTINGS_JSON.write_text(json.dumps(st, indent=1), encoding="utf-8")
    return jsonify(network_payload())


# ---------------------------------------------------------------- native window
WINDOW = None


class WindowApi:
    """Exposed to the page as window.pywebview.api.*"""

    def pick_folder(self):
        import webview
        picked = WINDOW.create_file_dialog(webview.FileDialog.FOLDER, directory=str(OUT_DIR))
        return picked[0] if picked else None


# ---------------------------------------------------------------- one-click logins (in-app browser window)
# The user signs in on the real site inside an app window. That window shares the app's WebView2
# profile, so the login is remembered and reconnecting later is instant. We only read the cookies
# the site itself sets; passwords never pass through this app.
LOGIN_SITES = {
    "instagram": {
        "name": "Instagram",
        "url": "https://www.instagram.com/accounts/login/",
        "done": {"sessionid"},
        "host": "instagram.com",
        "size": (480, 760),
    },
    "youtube": {
        "name": "YouTube",
        "url": "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F",
        "done": {"LOGIN_INFO", "SAPISID", "__Secure-3PAPISID"},
        "host": "youtube.com",   # finish once back on youtube.com; its cookies are what yt-dlp needs
        "size": (520, 760),
    },
}
if os.environ.get("MG_LOGIN_TEST") and os.environ.get("MG_DATA_DIR"):
    # test-only: finishes on a cookie Instagram sets for every visitor, so the capture can be tested without logging in
    LOGIN_SITES["igtest"] = {"name": "IG test", "url": "https://www.instagram.com/", "done": {"csrftoken"},
                             "host": "instagram.com", "size": (480, 600)}
LOGIN_STATE = {}   # site -> {"state": waiting|connected|cancelled|error, "message": str}


def cookie_to_line(name, morsel):
    """http.cookies.Morsel -> Netscape cookies.txt line."""
    from email.utils import parsedate_to_datetime
    domain = morsel["domain"] or ""
    expiry = 0
    try:
        dt = parsedate_to_datetime(morsel["expires"]) if morsel["expires"] else None
        if dt and dt.year > 1971:
            expiry = int(dt.timestamp())
    except (TypeError, ValueError):
        pass
    return "\t".join([domain, "TRUE" if domain.startswith(".") else "FALSE", morsel["path"] or "/",
                      "TRUE" if morsel["secure"] else "FALSE", str(expiry), name, morsel.value])


def run_login(site):
    import webview
    cfg = LOGIN_SITES[site]
    LOGIN_STATE[site] = {"state": "waiting", "message": f"Sign in to {cfg['name']} in the window that opened"}
    closed = threading.Event()
    win = webview.create_window(f"Log in to {cfg['name']}", cfg["url"], width=cfg["size"][0], height=cfg["size"][1])
    win.events.closed += closed.set
    deadline = time.time() + 15 * 60
    try:
        win.events.loaded.wait(30)
        while not closed.is_set() and time.time() < deadline:
            host = urllib.parse.urlparse(win.get_current_url() or "").netloc
            if cfg["host"] in host:
                found = {name: m for c in (win.get_cookies() or []) for name, m in c.items()}
                if cfg["done"] & {n for n, m in found.items() if m.value}:
                    lines = [cookie_to_line(n, m) for n, m in found.items() if m["domain"]]
                    store_cookie_lines(lines, replace_site=True)
                    LOGIN_STATE[site] = {"state": "connected", "message": f"Connected to {cfg['name']}"}
                    log.info("login captured for %s (%d cookies)", site, len(lines))
                    win.destroy()
                    return
            closed.wait(1.0)
        LOGIN_STATE[site] = {"state": "cancelled", "message": "The login window was closed before signing in"}
    except Exception as e:  # noqa: BLE001
        log.exception("login flow failed for %s", site)
        LOGIN_STATE[site] = {"state": "error", "message": str(e)[:200]}
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass


@app.post("/api/login/<site>")
def start_login(site):
    if site not in LOGIN_SITES:
        return jsonify(error="Unknown site"), 404
    if WINDOW is None:
        return jsonify(error="One-click login only works in the desktop app. Use Advanced to paste a cookie."), 400
    if LOGIN_STATE.get(site, {}).get("state") == "waiting":
        return jsonify(ok=True)   # window already open
    LOGIN_STATE[site] = {"state": "waiting", "message": "Opening…"}
    threading.Thread(target=run_login, args=(site,), daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/login/<site>")
def login_status(site):
    return jsonify(LOGIN_STATE.get(site) or {"state": "idle"})


INSTANCE_JSON = DATA_DIR / "instance.json"


def focus_existing_instance():
    """If the app is already open, bring its window forward instead of starting a second copy
    (two copies would fight over the same WebView2 profile and history file)."""
    try:
        port = json.loads(INSTANCE_JSON.read_text(encoding="utf-8"))["port"]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/focus", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=2).read()).get("app") == "media-grabber"
    except Exception:  # noqa: BLE001 - no running instance
        return False


@app.post("/api/focus")
def focus():
    if WINDOW is not None:
        try:
            WINDOW.restore()
            WINDOW.show()
            WINDOW.on_top = True     # flash to the front, then behave normally
            threading.Timer(0.4, lambda: setattr(WINDOW, "on_top", False)).start()
        except Exception:  # noqa: BLE001
            log.exception("focus failed")
    return jsonify(app="media-grabber")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_window():
    global WINDOW
    import webview
    from werkzeug.serving import make_server
    if focus_existing_instance():
        log.info("already running; focused the existing window")
        os._exit(0)
    port = free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    INSTANCE_JSON.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    theme_bg = "#0c0e13"
    WINDOW = webview.create_window("Media Grabber", f"http://127.0.0.1:{port}/", js_api=WindowApi(),
                                   width=1120, height=860, min_size=(440, 620), background_color=theme_bg)

    def on_closing():
        busy = [j for j in jobs.values() if j["status"] in ("queued", "downloading", "processing")]
        if busy:
            return WINDOW.create_confirmation_dialog(
                "Downloads in progress",
                f"{len(busy)} download{'s are' if len(busy) > 1 else ' is'} still running. Quit anyway?")
        return True

    WINDOW.events.closing += on_closing
    webview.start(private_mode=False, storage_path=str(DATA_DIR / "webview"))
    INSTANCE_JSON.unlink(missing_ok=True)
    os._exit(0)


if __name__ == "__main__":
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    TMP_DIR.mkdir(exist_ok=True)
    threading.Thread(target=check_updates, daemon=True).start()
    _st = load_settings()
    if _st.get("network_enabled") and _st.get("network_pin"):
        start_network(int(_st.get("network_port", 5180)))
    if FROZEN or "--window" in sys.argv:
        run_window()
    else:
        print(f"Media Grabber {VERSION} -> http://127.0.0.1:5177   (saving to {OUT_DIR}, data in {DATA_DIR})")
        app.run(host="127.0.0.1", port=5177, threaded=True)
