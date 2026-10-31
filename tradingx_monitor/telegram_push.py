"""Optional Telegram Bot API delivery; no requests are made until explicitly enabled.

The token is required in the API URL by Telegram, so arbitrary exception strings,
remote descriptions and request URLs must never be logged or returned as errors.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from .client import NoRedirect
from .images import ImageArchive
from .qq_push import format_parts
from .store import Store, encode, now
from .video_downloads import VideoDownloads, sha256_file


API = "https://api.telegram.org"
MAX_FILE_BYTES = 50_000_000
EVENTS = ["new_post", "updated", "video_new", "video_updated"]
DEFAULTS = {"enabled": False, "chat_id": "", "message_thread_id": None,
            "send_images": True, "send_video_files": False, "events": EVENTS}


class TelegramError(Exception):
    def __init__(self, code, uncertain=False, retry_after=0):
        self.code, self.uncertain, self.retry_after = code, uncertain, retry_after
        super().__init__("Telegram 接口错误（" + str(code) + "）")


def valid_chat(value):
    return isinstance(value, str) and bool(re.fullmatch(r"-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}", value))


def validate(settings):
    if not isinstance(settings, dict) or set(settings) - set(DEFAULTS):
        raise TelegramError("invalid_config")
    result = {**DEFAULTS, **settings}
    for key in ("enabled", "send_images", "send_video_files"):
        if type(result[key]) is not bool:
            raise TelegramError("invalid_config")
    if result["chat_id"] != "" and not valid_chat(result["chat_id"]):
        raise TelegramError("invalid_chat_id")
    if result["enabled"] and not valid_chat(result["chat_id"]):
        raise TelegramError("invalid_chat_id")
    topic = result["message_thread_id"]
    if topic is not None and (type(topic) is not int or topic <= 0):
        raise TelegramError("invalid_thread_id")
    if not isinstance(result["events"], list) or any(event not in EVENTS for event in result["events"]):
        raise TelegramError("invalid_events")
    return result


def load_settings(config):
    file = config.root / "telegram_bot.json"
    try:
        return validate(json.loads(file.read_text()) if file.exists() else {})
    except (OSError, ValueError):
        raise TelegramError("invalid_config") from None


def validate_token(token):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,128}", token):
        raise TelegramError("invalid_token")
    return token


def read_token(config):
    file = config.root / "secrets" / "telegram_bot_token"
    if not file.is_file():
        raise TelegramError("token_missing")
    if file.stat().st_mode & 0o077:
        raise TelegramError("token_permissions")
    return validate_token(file.read_text().strip())


def configure_settings(config, chat_id, token, thread_id=None, send_video_files=False):
    settings = validate({**DEFAULTS, "chat_id": chat_id, "message_thread_id": thread_id,
                         "send_video_files": send_video_files})
    if not valid_chat(chat_id):
        raise TelegramError("invalid_chat_id")
    validate_token(token)
    files = (config.root / "secrets" / "telegram_bot_token", config.root / "telegram_bot.json")
    if any(file.exists() for file in files):
        raise TelegramError("configuration_exists")
    temporary, published = [], []
    try:
        for file, content in zip(files, (token, encode(settings) + "\n")):
            descriptor, name = tempfile.mkstemp(prefix=".telegram-setup-", suffix=".tmp", dir=file.parent)
            temporary.append(Path(name))
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content); handle.flush(); os.fsync(handle.fileno())
            os.link(name, file)
            published.append(file)
    except BaseException:
        for file in published: file.unlink(missing_ok=True)
        raise
    finally:
        for file in temporary: file.unlink(missing_ok=True)
    return {"configured": True, "enabled": False, "token_displayed": False,
            "send_video_files": send_video_files}


def ready(config):
    try:
        return load_settings(config)["enabled"] and (config.root / "secrets/telegram_bot_token").is_file()
    except TelegramError:
        return False


def videos_enabled(config):
    try:
        settings = load_settings(config)
        return settings["enabled"] and settings["send_video_files"]
    except TelegramError:
        return False


def split_text(text, limit=4000):
    """Use UTF-16 units conservatively and never split a Unicode character."""
    parts, current, size = [], [], 0
    for character in text:
        units = len(character.encode("utf-16-le")) // 2
        if current and size + units > limit:
            parts.append("".join(current)); current, size = [], 0
        current.append(character); size += units
    if current: parts.append("".join(current))
    return parts or [""]


class MultipartBody:
    """Bounded-memory multipart upload with a known Content-Length."""
    def __init__(self, fields, field, path, filename, content_type):
        self.path = Path(path)
        self.boundary = "tradingx-" + secrets.token_hex(16)
        blocks = []
        for name, value in fields.items():
            blocks.append((f'--{self.boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
        filename = re.sub(r'["\\\x00-\x1f\x7f]', "_", filename)
        blocks.append((f'--{self.boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n').encode())
        self.prefix = b"".join(blocks)
        self.suffix = (f"\r\n--{self.boundary}--\r\n").encode()
        self.size = len(self.prefix) + self.path.stat().st_size + len(self.suffix)

    def __iter__(self):
        yield self.prefix
        with self.path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""): yield chunk
        yield self.suffix


class TelegramAPI:
    def __init__(self, config, opener=None):
        self.config = config
        self.opener = opener or build_opener(NoRedirect())

    def _request(self, method, fields, file=None, sending=True):
        if method not in ("getMe", "sendMessage", "sendDocument", "sendVideo"):
            raise TelegramError("invalid_method")
        token = read_token(self.config)
        headers = {"Accept": "application/json", "User-Agent": "TradingX-Local-Monitor/1.0"}
        if file:
            field, path, filename, content_type = file
            if not 0 < Path(path).stat().st_size <= MAX_FILE_BYTES:
                raise TelegramError("file_too_large")
            body = MultipartBody(fields, field, path, filename, content_type)
            headers.update({"Content-Type": "multipart/form-data; boundary=" + body.boundary,
                            "Content-Length": str(body.size)})
        else:
            body = encode(fields).encode()
            headers["Content-Type"] = "application/json"
        request = Request(API + "/bot" + token + "/" + method, data=body, headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=120 if file else 20) as response:
                raw = response.read(1_000_001)
        except HTTPError as error:
            try: payload = json.loads(error.read(1_000_000))
            except Exception: payload = {}
            retry = payload.get("parameters", {}).get("retry_after", 60) if isinstance(payload, dict) and isinstance(payload.get("parameters", {}), dict) else 60
            if error.code == 429:
                raise TelegramError("rate_limited", retry_after=retry if type(retry) is int and retry > 0 else 60) from None
            raise TelegramError("http_" + str(error.code), sending and error.code >= 500) from None
        except Exception:
            raise TelegramError("network_error", sending) from None
        try:
            if len(raw) > 1_000_000: raise ValueError()
            payload = json.loads(raw)
            if not isinstance(payload, dict): raise ValueError()
        except (ValueError, TypeError):
            raise TelegramError("invalid_response", sending) from None
        if payload.get("ok") is not True:
            code = payload.get("error_code")
            if code == 429:
                params = payload.get("parameters", {})
                retry = params.get("retry_after", 60) if isinstance(params, dict) else 60
                raise TelegramError("rate_limited", retry_after=retry if type(retry) is int and retry > 0 else 60)
            raise TelegramError("api_" + str(code) if type(code) is int else "api_error", sending and (type(code) is not int or code >= 500))
        result = payload.get("result")
        if not isinstance(result, dict): raise TelegramError("invalid_response", sending)
        if sending and (type(result.get("message_id")) is not int or result["message_id"] <= 0):
            raise TelegramError("send_ack_missing", True)
        return result

    def _fields(self, chat_id, thread_id):
        if not valid_chat(chat_id): raise TelegramError("invalid_chat_id")
        fields = {"chat_id": chat_id}
        if thread_id is not None: fields["message_thread_id"] = thread_id
        return fields

    def send_message(self, chat_id, text, thread_id=None):
        if not text or len(text.encode("utf-16-le")) // 2 > 4096:
            raise TelegramError("invalid_message_length")
        fields = {**self._fields(chat_id, thread_id), "text": text, "link_preview_options": {"is_disabled": True}}
        return self._request("sendMessage", fields)["message_id"]

    def send_file(self, chat_id, path, filename, caption, thread_id=None, video=False):
        caption = split_text(caption, 1024)[0]
        fields = {**self._fields(chat_id, thread_id), "caption": caption}
        if video: fields["supports_streaming"] = "true"
        field, method = ("video", "sendVideo") if video else ("document", "sendDocument")
        return self._request(method, fields, (field, path, filename, "video/mp4" if video else "application/octet-stream"))["message_id"]

    def doctor(self):
        data = self._request("getMe", {}, sending=False)
        if data.get("is_bot") is not True: raise TelegramError("invalid_bot_identity")
        return {"authenticated": True, "is_bot": True}


def destination(settings):
    return hashlib.sha256(encode([settings["chat_id"], settings["message_thread_id"]]).encode()).hexdigest()


class TelegramQueue:
    def __init__(self, store):
        self.store, self.config = store, store.config
        self.images, self.videos = ImageArchive(store), VideoDownloads(store)
        with store.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS telegram_outbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER NOT NULL,part INTEGER NOT NULL,
                target_key TEXT NOT NULL,chat_id TEXT NOT NULL,thread_id INTEGER,content TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text',asset_key TEXT,status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0,error_code TEXT,
                message_id INTEGER,updated_at TEXT NOT NULL,UNIQUE(event_id,part,target_key));
                CREATE UNIQUE INDEX IF NOT EXISTS telegram_media_dedup ON telegram_outbox(target_key,asset_key)
                WHERE message_type IN ('image','video_file','video_link');""")

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE telegram_outbox SET status='uncertain',error_code='worker_interrupted' WHERE status='sending'")

    def _insert(self, db, settings, event_id, part, content, kind="text", asset_key=None):
        db.execute("INSERT OR IGNORE INTO telegram_outbox(event_id,part,target_key,chat_id,thread_id,content,message_type,asset_key,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (event_id, part, destination(settings), settings["chat_id"], settings["message_thread_id"], content, kind, asset_key, now()))

    def enqueue(self):
        settings = load_settings(self.config)
        if not settings["enabled"]: return
        target = destination(settings)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store._state(db)
            if "telegram_event_cursor" not in state:
                cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
                self.store._update(db, telegram_event_cursor=cursor, telegram_event_floor=cursor,
                                   telegram_destination_hash=target)
                return
            if state.get("telegram_destination_hash") != target:
                raise TelegramError("destination_changed")
            events = db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 1000", (state["telegram_event_cursor"],)).fetchall()
            for event in events:
                if event["event_type"] not in settings["events"]: continue
                if event["event_type"] in ("video_new", "video_updated"):
                    from .videos import format_video
                    text = format_video(json.loads(event["details_json"])["video"], event["event_type"], event["observed_at"])
                    for part, content in enumerate(split_text(text), 1): self._insert(db, settings, event["id"], part, content, "video")
                else:
                    row = db.execute("SELECT payload_json FROM revisions WHERE post_id=? AND observed_at<=? ORDER BY id DESC LIMIT 1", (event["post_id"], event["observed_at"])).fetchone()
                    if not row: continue
                    post = json.loads(row[0]); part = 0
                    for text in format_parts(post, event["event_type"]):
                        for content in split_text(text):
                            part += 1; self._insert(db, settings, event["id"], part, content)
                    if settings["send_images"] and not post["locked"]:
                        for index, image in enumerate(post["images"], 1):
                            key = self.images.register(post, image, db)
                            if key:
                                part += 1; self._insert(db, settings, event["id"], part, f"TradingX #{post['id']} 原图 {index}", "image", key)
            if events: self.store._update(db, telegram_event_cursor=events[-1]["id"])
            if settings["send_video_files"]:
                rows = db.execute("SELECT j.event_id,j.video_json,j.asset_key,f.size FROM video_download_jobs j JOIN video_files f ON f.asset_key=j.asset_key WHERE j.status='ready' AND j.event_id>? AND NOT EXISTS(SELECT 1 FROM telegram_outbox o WHERE o.target_key=? AND o.asset_key=j.asset_key) ORDER BY j.id LIMIT 100", (state["telegram_event_floor"], target)).fetchall()
                for row in rows:
                    video = json.loads(row["video_json"])
                    title = f"第{video['number']}期 · " if video.get("number") else ""
                    title += video["title"]
                    oversized = row["size"] > MAX_FILE_BYTES
                    text = title + ("\n文件超过 Telegram 云端 50 MB 上传上限，本次仅发送观看链接；本地完整 MP4 已保留。" if oversized else "\n完整 MP4") + "\n" + video["url"]
                    self._insert(db, settings, row["event_id"], 100000, text, "video_link" if oversized else "video_file", row["asset_key"])

    def deliver_one(self, api):
        settings = load_settings(self.config)
        if not settings["enabled"]: return False
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store._state(db)
            if state.get("telegram_destination_hash") != destination(settings): raise TelegramError("destination_changed")
            if db.execute("SELECT 1 FROM telegram_outbox WHERE status IN ('failed','uncertain','sending') LIMIT 1").fetchone(): return False
            row = db.execute("SELECT * FROM telegram_outbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row is None or row["retry_at"] > time.time(): return False
            if row["target_key"] != destination(settings): raise TelegramError("destination_changed")
            image = self.images.read(row["asset_key"]) if row["message_type"] == "image" else None
            if row["message_type"] == "image" and not image: return False
            asset = self.videos.asset(row["asset_key"]) if row["message_type"] == "video_file" else None
            if row["message_type"] == "video_file" and not asset:
                db.execute("UPDATE telegram_outbox SET status='failed',error_code='video_file_missing',updated_at=? WHERE id=?", (now(), row["id"]))
                return False
            db.execute("UPDATE telegram_outbox SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?", (now(), row["id"]))
        try:
            if image:
                ack = api.send_file(row["chat_id"], self.images.folder / image[2], image[2], row["content"], row["thread_id"])
            elif asset:
                if sha256_file(asset["path"]) != asset["sha256"]: raise TelegramError("video_file_checksum_failed")
                ack = api.send_file(row["chat_id"], asset["path"], asset["display_name"], row["content"], row["thread_id"], video=True)
            else:
                ack = api.send_message(row["chat_id"], row["content"], row["thread_id"])
            if type(ack) is not int or ack <= 0: raise TelegramError("send_ack_missing", True)
        except TelegramError as error:
            with self.store.connect() as db:
                status = "pending" if error.retry_after else "uncertain" if error.uncertain else "failed"
                db.execute("UPDATE telegram_outbox SET status=?,error_code=?,retry_at=?,updated_at=? WHERE id=?", (status, error.code, time.time() + error.retry_after, now(), row["id"]))
                self.store._update(db, telegram_last_error=error.code)
            return False
        with self.store.connect() as db:
            db.execute("UPDATE telegram_outbox SET status='sent',error_code=NULL,message_id=?,updated_at=? WHERE id=?", (ack, now(), row["id"]))
            self.store._update(db, telegram_last_error=None, telegram_last_sent=now())
        return True

    def retry(self, delivery):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM telegram_outbox WHERE id=?", (delivery,)).fetchone()
            if not row or row["status"] not in ("failed", "uncertain"): raise TelegramError("delivery_not_retryable")
            if row["target_key"] != destination(load_settings(self.config)): raise TelegramError("destination_changed")
            db.execute("UPDATE telegram_outbox SET status='pending',retry_at=0,error_code=NULL,updated_at=? WHERE id=?", (now(), delivery))

    def status(self, redact_target=False):
        settings = load_settings(self.config)
        with self.store.connect() as db:
            counts = dict(db.execute("SELECT status,COUNT(*) FROM telegram_outbox GROUP BY status").fetchall())
            sent = dict(db.execute("SELECT message_type,COUNT(*) FROM telegram_outbox WHERE status='sent' GROUP BY message_type").fetchall())
            paused_error = db.execute("SELECT error_code FROM telegram_outbox WHERE status IN ('failed','uncertain','sending') ORDER BY id DESC LIMIT 1").fetchone()
            state = self.store._state(db)
        result = {"configured": (self.config.root / "telegram_bot.json").exists(), "enabled": settings["enabled"],
                  "token_configured": (self.config.root / "secrets/telegram_bot_token").is_file(), "target_label": "Telegram 机器人对话框",
                  "outbox": counts, "sent_text": sent.get("text", 0), "sent_images": sent.get("image", 0),
                  "sent_video_notifications": sent.get("video", 0), "sent_video_files": sent.get("video_file", 0),
                  "sent_video_links": sent.get("video_link", 0), "last_error": (paused_error[0] or "delivery_paused") if paused_error else state.get("telegram_last_error"),
                  "paused": bool(paused_error),
                  "last_sent": state.get("telegram_last_sent"), "worker_heartbeat": state.get("telegram_heartbeat"),
                  "send_video_files": settings["send_video_files"], "max_file_bytes": MAX_FILE_BYTES,
                  "live_delivery_verified": bool(state.get("telegram_last_sent") or state.get("telegram_last_test"))}
        if not redact_target: result.update(chat_id=settings["chat_id"], message_thread_id=settings["message_thread_id"])
        return result


def run_telegram(config):
    from .runner import lock
    if not ready(config): raise TelegramError("not_enabled_or_token_missing")
    store = Store(config); queue = TelegramQueue(store); api = TelegramAPI(config)
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM): signal.signal(signum, lambda *_: stopped.set())
    with lock(config, "telegram_worker.lock"):
        queue.recover()
        while not stopped.is_set():
            with store.connect() as db: store._update(db, telegram_heartbeat=now())
            try:
                queue.enqueue(); queue.deliver_one(api)
            except TelegramError as error:
                with store.connect() as db: store._update(db, telegram_last_error=error.code)
            except Exception:
                with store.connect() as db: store._update(db, telegram_last_error="worker_error")
            stopped.wait(5)
