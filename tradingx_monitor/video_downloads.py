"""Authorized Cloudflare HLS -> local MP4, following Deal-Data's download approach.

Playback credentials are acquired from the site's normal member API, rather than
from a manually exported HAR. Signed URLs stay in private temporary playlists;
they are never command arguments, API status fields, or log messages.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, build_opener

from .client import Client, FetchError, NoRedirect, read_token
from .config import SOURCE
from .store import encode, now


STREAM_HOST = "customer-6chy9ubzlkinqim7.cloudflarestream.com"
QQ_MAX_BYTES = 195_000_000
MAX_CAPTURE_BYTES = 1_000_000_000


class VideoDownloadError(Exception):
    def __init__(self, code):
        self.code = str(code)
        super().__init__("视频任务错误（" + self.code + "）")


def video_filename(video):
    number = video.get("number")
    prefix = f"第{number}期" if type(number) is int and number > 0 else f"视频{video['id']}"
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(video.get("title") or "")).strip(" ._")[:55]
    return prefix + ("_" + title if title else "") + ".mp4"


def attrs(line):
    return {m[1]: m[2].strip('"') for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[-1])}


def trusted_stream_url(url):
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        valid = (p.scheme == "https" and not p.username and not p.password and p.port in (None, 443)
                 and (host == "videodelivery.net" or host.endswith(".videodelivery.net") or host.endswith(".cloudflarestream.com")))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise VideoDownloadError("untrusted_stream_url")
    return url


def private_write(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)


def localize_playlist(text, base):
    if not text.startswith("#EXTM3U") or "#EXT-X-ENDLIST" not in text:
        raise VideoDownloadError("not_complete_vod")
    output = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY"):
            if attrs(line).get("METHOD") not in ("NONE", "AES-128"):
                raise VideoDownloadError("drm_not_supported")
        if line and not line.startswith("#"):
            line = trusted_stream_url(urljoin(base, line))
        else:
            line = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + trusted_stream_url(urljoin(base, m[1])) + '"', line)
        output.append(line)
    duration = sum(float(line.split(":", 1)[1].split(",", 1)[0]) for line in output if line.startswith("#EXTINF:"))
    if duration <= 0 or duration > 4 * 3600:
        raise VideoDownloadError("invalid_video_duration")
    return "\n".join(output) + "\n", duration


def stream_manifest(url, opener=None):
    trusted_stream_url(url)
    try:
        with (opener or build_opener(NoRedirect())).open(Request(url, headers={"User-Agent": "TradingX-Local-Monitor/1.0"}), timeout=20) as response:
            data = response.read(1_000_001)
        if len(data) > 1_000_000:
            raise VideoDownloadError("manifest_too_large")
        text = data.decode("utf-8-sig")
        if not text.startswith("#EXTM3U"):
            raise VideoDownloadError("invalid_manifest")
        return text
    except HTTPError as error:
        raise VideoDownloadError("stream_http_" + str(error.code)) from None
    except (URLError, OSError, UnicodeDecodeError, FetchError):
        raise VideoDownloadError("stream_fetch_failed") from None


def prepare_playlists(master, base, folder, duration_hint, fetch=stream_manifest):
    lines = master.splitlines()
    variants, audio = [], {}
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:"):
            entry = attrs(line)
            if entry.get("TYPE") == "AUDIO" and entry.get("URI"):
                group = entry.get("GROUP-ID", "")
                if group not in audio or entry.get("DEFAULT") == "YES":
                    audio[group] = (line, trusted_stream_url(urljoin(base, entry["URI"])))
        if line.startswith("#EXT-X-STREAM-INF:") and index + 1 < len(lines):
            entry = attrs(line)
            try:
                height = int(entry.get("RESOLUTION", "0x0").split("x")[-1])
                bandwidth = int(entry.get("AVERAGE-BANDWIDTH") or entry.get("BANDWIDTH", 0))
            except ValueError:
                raise VideoDownloadError("invalid_variant") from None
            variants.append((height, bandwidth, line, trusted_stream_url(urljoin(base, lines[index + 1])), entry.get("AUDIO")))
    if variants:
        candidates = [v for v in variants if v[0] <= 1080 and (not duration_hint or v[1] * duration_hint / 8 < QQ_MAX_BYTES * .9)]
        selected = max(candidates or variants, key=lambda v: (v[0], v[1])) if candidates else min(variants, key=lambda v: (v[1], v[0]))
        video_text, duration = localize_playlist(fetch(selected[3]), selected[3])
        private_write(folder / "video.m3u8", video_text)
        result = ["#EXTM3U", "#EXT-X-VERSION:6"]
        has_audio = selected[4] in audio
        if has_audio:
            audio_line, audio_url = audio[selected[4]]
            audio_text, _ = localize_playlist(fetch(audio_url), audio_url)
            private_write(folder / "audio.m3u8", audio_text)
            result.append(re.sub(r'URI="[^"]+"', 'URI="audio.m3u8"', audio_line))
        result.extend([selected[2], "video.m3u8"])
        private_write(folder / "master.m3u8", "\n".join(result) + "\n")
        return folder / "master.m3u8", duration, has_audio
    text, duration = localize_playlist(master, base)
    private_write(folder / "video.m3u8", text)
    return folder / "video.m3u8", duration, False


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_video(path):
    probe = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    try:
        result = subprocess.run([probe, "-v", "error", "-protocol_whitelist", "file", "-show_format", "-show_streams", "-of", "json", str(path)],
                                capture_output=True, timeout=30, check=True)
        data = json.loads(result.stdout)
        videos = [s for s in data["streams"] if s.get("codec_type") == "video"]
        if not videos:
            raise ValueError()
        return {"duration": float(data["format"]["duration"]), "width": int(videos[0]["width"]),
                "height": int(videos[0]["height"]), "audio": any(s.get("codec_type") == "audio" for s in data["streams"])}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise VideoDownloadError("invalid_downloaded_video") from None


class VideoDownloads:
    def __init__(self, store):
        self.store, self.config = store, store.config
        self.folder = self.config.root / "data" / "videos"
        self.folder.mkdir(mode=0o700, exist_ok=True);self.folder.chmod(0o700)
        self.stopped = threading.Event()
        self.process = None
        with store.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS video_download_jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER UNIQUE NOT NULL,video_id INTEGER NOT NULL,
                video_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',asset_key TEXT,error_code TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0,progress REAL NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS video_files(asset_key TEXT PRIMARY KEY,video_id INTEGER NOT NULL,
                filename TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,duration REAL NOT NULL,
                width INTEGER NOT NULL,height INTEGER NOT NULL,has_audio INTEGER NOT NULL,created_at TEXT NOT NULL);
            """)

    def enabled(self):
        from .qq_push import QQError, load_settings
        from .telegram_push import videos_enabled
        if (self.config.root / "qq_bot.json").exists():
            try:
                if load_settings(self.config).get("send_video_files", False): return True
            except (QQError, OSError, ValueError):
                pass
        return videos_enabled(self.config)

    def request(self, video_id):
        from .videos import VideoMonitor
        if not self.enabled():
            raise VideoDownloadError("video_download_disabled")
        videos = [v for v in VideoMonitor(self.store).listing(10000) if v["id"] == video_id]
        if not videos:
            raise VideoDownloadError("video_not_found")
        with self.store.connect() as db:
            state = self.store._state(db)
            if "video_download_cursor" not in state:
                self.store._update(db, video_download_cursor=db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
            self.store._event(db, now(), "video_download_requested", video=videos[0])
        self.enqueue()

    def enqueue(self):
        if not self.enabled():return
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store._state(db)
            if "video_download_cursor" not in state:
                self.store._update(db, video_download_cursor=db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]);return
            events = db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 1000", (state["video_download_cursor"],)).fetchall()
            for event in events:
                if event["event_type"] in ("video_new", "video_updated", "video_download_requested"):
                    video = json.loads(event["details_json"])["video"]
                    db.execute("INSERT OR IGNORE INTO video_download_jobs(event_id,video_id,video_json,updated_at) VALUES(?,?,?,?)",
                               (event["id"], video["id"], encode(video), now()))
            if events:self.store._update(db, video_download_cursor=events[-1]["id"])

    def asset(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):return None
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM video_files WHERE asset_key=?", (key,)).fetchone()
            job = db.execute("SELECT video_json FROM video_download_jobs WHERE asset_key=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        if not row:return None
        result = dict(row)
        if result["filename"] != key + ".mp4":return None
        path = self.folder / result["filename"]
        if not path.is_file() or path.stat().st_size != result["size"]:return None
        video = json.loads(job[0]) if job else {"id":result["video_id"]}
        result.update(number=video.get("number"), title=video.get("title"), display_name=video_filename(video))
        result["path"] = path
        return result

    def _progress(self, job_id, value):
        with self.store.connect() as db:
            db.execute("UPDATE video_download_jobs SET progress=?,updated_at=? WHERE id=?", (min(99, value), now(), job_id))
            self.store._update(db, video_download_heartbeat=now())

    def _ffmpeg(self, arguments, job_id, duration, output):
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        command = [ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1"] + arguments + [str(output)]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            last_update = 0
            for line in self.process.stdout:
                if self.stopped.is_set():self.process.terminate()
                if time.monotonic() - last_update >= 5 and line.startswith("out_time_us="):
                    try:self._progress(job_id, int(line.strip().split("=", 1)[1]) / 1_000_000 / duration * 100)
                    except ValueError:pass
                    last_update = time.monotonic()
                if output.exists() and output.stat().st_size > MAX_CAPTURE_BYTES:
                    self.process.terminate();raise VideoDownloadError("video_capture_too_large")
            if self.process.wait() != 0:raise VideoDownloadError("ffmpeg_failed")
            if self.stopped.is_set():raise VideoDownloadError("interrupted")
        except OSError:
            raise VideoDownloadError("ffmpeg_unavailable") from None
        finally:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
            self.process = None

    def download_one(self):
        if not self.enabled():return False
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM video_download_jobs WHERE status IN ('pending','failed') AND retry_at<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
            if not row:return False
            job = dict(row)
            db.execute("UPDATE video_download_jobs SET status='downloading',attempts=attempts+1,error_code=NULL,progress=0,updated_at=? WHERE id=?", (now(), job["id"]))
        workspace = self.folder / (".job-" + str(job["id"]))
        try:
            shutil.rmtree(workspace, ignore_errors=True);workspace.mkdir(mode=0o700)
            playback = Client(self.config)._get(SOURCE + "/api/video-stream?episode=" + str(job["video_id"]), read_token(self.config))
            token, stream_id = playback.get("signedToken"), playback.get("cfStreamId")
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{20,16384}", token) or not isinstance(stream_id, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", stream_id):
                raise VideoDownloadError("playback_not_authorized")
            key = hashlib.sha256((str(job["video_id"]) + ":" + stream_id).encode()).hexdigest()
            asset = self.asset(key)
            video = json.loads(job["video_json"])
            if not asset:
                # Same signed playback endpoint as the provided Deal-Data custom-domain fallback.
                url = "https://" + STREAM_HOST + "/" + quote(token, safe="") + "/manifest/video.m3u8"
                master = stream_manifest(url)
                hint = 0
                try:
                    for part in video["duration"].split(":"):hint = hint * 60 + int(part)
                except (ValueError, KeyError):hint = 0
                playlist, duration, needs_audio = prepare_playlists(master, url, workspace, hint)
                output = workspace / "capture.mp4"
                self._ffmpeg(["-protocol_whitelist", "file,https,tls,tcp,crypto", "-allowed_extensions", "ALL", "-i", str(playlist),
                              "-c", "copy", "-movflags", "+faststart"], job["id"], duration, output)
                info = probe_video(output)
                if abs(info["duration"] - duration) > max(3, duration * .02) or (needs_audio and not info["audio"]):
                    raise VideoDownloadError("incomplete_video")
                if output.stat().st_size > QQ_MAX_BYTES:
                    target_bitrate = int(QQ_MAX_BYTES * .9 * 8 / duration) - 96000
                    if target_bitrate < 150000:raise VideoDownloadError("video_too_large_for_qq")
                    smaller = workspace / "share.mp4"
                    self._ffmpeg(["-i", str(output), "-c:v", "libx264", "-preset", "fast", "-b:v", str(target_bitrate),
                        "-maxrate", str(target_bitrate), "-bufsize", str(target_bitrate * 2), "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart"], job["id"], duration, smaller)
                    output = smaller;info = probe_video(output)
                size = output.stat().st_size
                if size > QQ_MAX_BYTES:raise VideoDownloadError("video_too_large_for_qq")
                checksum = sha256_file(output);filename = key + ".mp4"
                os.replace(output, self.folder / filename);(self.folder / filename).chmod(0o600)
                with self.store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO video_files VALUES(?,?,?,?,?,?,?,?,?,?)", (key, job["video_id"], filename, size, checksum,
                        info["duration"], info["width"], info["height"], int(info["audio"]), now()))
            with self.store.connect() as db:
                db.execute("UPDATE video_download_jobs SET status='ready',asset_key=?,error_code=NULL,retry_at=0,progress=100,updated_at=? WHERE id=?", (key, now(), job["id"]))
                self.store._event(db, now(), "video_file_ready", video=video, asset_key=key)
            return True
        except (FetchError, VideoDownloadError) as error:
            code = error.code
        except Exception:
            code = "video_download_failed"
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        with self.store.connect() as db:
            db.execute("UPDATE video_download_jobs SET status='failed',error_code=?,retry_at=?,updated_at=? WHERE id=?",
                (code, time.time() + min(3600, 120 * 2 ** min(job["attempts"], 5)), now(), job["id"]))
        return False

    def status(self):
        with self.store.connect() as db:
            counts = {r[0]:r[1] for r in db.execute("SELECT status,COUNT(*) FROM video_download_jobs GROUP BY status")}
            files, size = db.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM video_files").fetchone()
            latest = db.execute("SELECT id,video_id,status,progress,error_code,attempts,updated_at FROM video_download_jobs ORDER BY id DESC LIMIT 1").fetchone()
            state = self.store._state(db)
            file = db.execute("SELECT asset_key,video_id,size,duration,width,height,has_audio,created_at FROM video_files ORDER BY created_at DESC LIMIT 1").fetchone()
        latest_file = dict(file) if file else None
        if latest_file:
            latest_file["local_url"] = "/api/video-files/"+latest_file["asset_key"]
            asset = self.asset(latest_file["asset_key"])
            if asset:latest_file.update({k:asset[k] for k in ("number","title","display_name")})
        return {"enabled": self.enabled(), "jobs": counts, "downloaded_files":files,"bytes":size,
            "latest_job":dict(latest) if latest else None,"latest_file":latest_file,"worker_heartbeat":state.get("video_download_heartbeat"),"qq_max_bytes":QQ_MAX_BYTES}


def run_downloads(config):
    from .runner import lock
    from .store import Store
    store = Store(config);downloads = VideoDownloads(store)
    def stop(*_):
        downloads.stopped.set()
        if downloads.process and downloads.process.poll() is None:downloads.process.terminate()
    for signum in (signal.SIGINT, signal.SIGTERM):signal.signal(signum, stop)
    with lock(config, "video_download_worker.lock"):
        with store.connect() as db:
            db.execute("UPDATE video_download_jobs SET status='pending',error_code='worker_interrupted',retry_at=0 WHERE status='downloading'")
        while not downloads.stopped.is_set():
            with store.connect() as db:store._update(db, video_download_heartbeat=now())
            try:
                downloads.enqueue();downloads.download_one()
            except Exception:
                with store.connect() as db:store._update(db, video_download_worker_error="worker_error")
            downloads.stopped.wait(5)
