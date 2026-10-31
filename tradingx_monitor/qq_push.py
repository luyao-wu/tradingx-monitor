"""QQ official API, persisted delivery queue, and explicit group binding."""
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener
from zoneinfo import ZoneInfo

from .client import FetchError, NoRedirect
from .images import ImageArchive, MAX_IMAGE_BYTES
from .store import encode, now


API = "https://api.bot.qq.com"


class QQError(Exception):
    def __init__(self, code, uncertain=False):
        super().__init__(f"QQ 接口错误（{code}）")
        self.code = str(code)
        self.uncertain = uncertain


def load_settings(config):
    filename = config.root / "qq_bot.json"
    data = json.loads(filename.read_text()) if filename.exists() else {}
    if not isinstance(data, dict):
        raise QQError("invalid_config")
    for key in ("appid", "target_group_number"):
        if not isinstance(data.get(key), str) or not data[key].isdigit():
            raise QQError("invalid_config")
    if not isinstance(data.get("group_openid", ""), str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,128}", data.get("group_openid", "")):
        raise QQError("invalid_group_openid")
    events = data.get("events", [])
    if type(data.get("enabled", False)) is not bool or not isinstance(events, list) or any(event not in ("new_post", "updated", "video_new", "video_updated") for event in events):
        raise QQError("invalid_config")
    if type(data.get("send_images", True)) is not bool:
        raise QQError("invalid_config")
    if type(data.get("send_video_files", False)) is not bool:
        raise QQError("invalid_config")
    return data


def save_settings(config, data):
    filename = config.root / "qq_bot.json"
    temporary = filename.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, filename)


def read_secret(config):
    filename = config.root / "secrets" / "qq_app_secret"
    if not filename.exists():
        raise QQError("secret_missing")
    if filename.stat().st_mode & 0o077:
        raise QQError("secret_permissions")
    value = filename.read_text().strip()
    if not value or any(c.isspace() for c in value) or len(value) > 1024:
        raise QQError("invalid_secret")
    return value


class QQAPI:
    def __init__(self, config, settings=None, opener=None):
        self.config = config
        self.settings = settings or load_settings(config)
        self.opener = opener or build_opener(NoRedirect())
        self.token = None
        self.token_until = 0

    def _request(self, path, body=None, authenticated=True, sending=False, timeout=20):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "QQBot " + self.access_token()
        request = Request(API + path, data=encode(body).encode() if body is not None else None,
                          headers=headers, method="POST" if body is not None else "GET")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise QQError("response_too_large", sending)
        except HTTPError as error:
            try:
                payload = json.loads(error.read(64 * 1024))
                code = payload.get("code", error.code)
                code = code if type(code) is int else error.code
            except (ValueError, AttributeError):
                code = error.code
            if error.code == 401:
                self.token = None
            raise QQError(code) from None
        except (URLError, OSError, TimeoutError):
            raise QQError("network_error", sending) from None
        except Exception as error:
            if isinstance(error, QQError):
                raise
            raise QQError("request_failed", sending) from None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise QQError("invalid_response", sending) from None
        if not isinstance(payload, dict):
            raise QQError("invalid_response", sending)
        if payload.get("code") not in (None, 0):
            code = payload["code"] if type(payload["code"]) is int else "api_error"
            raise QQError(code)
        return payload

    def access_token(self):
        if self.token and time.time() < self.token_until:
            return self.token
        data = self._request("/app/getAppAccessToken", {"appId": self.settings["appid"],
                             "clientSecret": read_secret(self.config)}, authenticated=False)
        if not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise QQError("token_invalid")
        try:
            lifetime = int(data["expires_in"])
        except (KeyError, ValueError, TypeError):
            raise QQError("token_invalid") from None
        self.token = data["access_token"]
        self.token_until = time.time() + max(1, lifetime - 60)
        return self.token

    def send(self, group_openid, content, reply_id=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", group_openid) or group_openid == self.settings["target_group_number"]:
            raise QQError("group_not_bound")
        payload = {"msg_type": 0, "content": content}
        if reply_id:
            payload["msg_id"] = reply_id
        data = self._request(f"/v2/groups/{quote(group_openid, safe='')}/messages", payload, sending=True)
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise QQError("send_ack_missing", True)
        return data["id"]

    def send_image(self, group_openid, filename, image, caption=""):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", group_openid) or group_openid == self.settings["target_group_number"]:
            raise QQError("group_not_bound")
        if not 0 < len(image) <= MAX_IMAGE_BYTES:
            raise QQError("image_too_large")
        group_path = "/v2/groups/" + quote(group_openid, safe="")
        body = {"file_type": 1, "file_name": filename, "srv_send_msg": False}
        if len(image) <= 3_500_000:
            body["file_data"] = base64.b64encode(image).decode("ascii")
        else:
            prepared = self._request(group_path + "/upload_prepare", {
                "file_type": 1, "file_name": filename, "file_size": str(len(image)),
                "md5": hashlib.md5(image).hexdigest(), "sha1": hashlib.sha1(image).hexdigest(),
                "md5_10m": hashlib.md5(image[:10002432]).hexdigest()})
            upload_id, parts = prepared.get("upload_id"), prepared.get("parts")
            if not isinstance(upload_id, str) or not isinstance(parts, list) or not 1 <= len(parts) <= 100:
                raise QQError("invalid_upload_prepare")
            first_index = parts[0].get("index") if isinstance(parts[0],dict) else None
            if type(first_index) is not int or first_index not in (0,1):raise QQError("invalid_upload_part")
            offset = 0
            for index, part in enumerate(parts):
                try:
                    size = int(part.get("block_size", prepared.get("block_size", "0")))
                    signed_url = part["presigned_url"]
                    url = urlsplit(signed_url)
                    hostname = url.hostname or ""
                    if (part["index"] != index + first_index or size <= 0 or size > MAX_IMAGE_BYTES
                            or url.scheme != "https" or url.username or url.password
                            or url.port not in (None, 443)
                            or not any(hostname.endswith(suffix) for suffix in (".myqcloud.com", ".tencentcos.cn"))):
                        raise ValueError()
                    chunk = image[offset:offset + size]
                    if not chunk:
                        raise ValueError()
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise QQError("invalid_upload_part") from None
                # The QQ token and member token are never attached to a COS upload URL.
                try:
                    with self.opener.open(Request(signed_url, data=chunk, method="PUT",
                                                  headers={"Content-Type": "application/octet-stream"}), timeout=20) as response:
                        response.read(1024)
                except Exception:
                    raise QQError("image_part_upload_failed") from None
                self._request(group_path + "/upload_part_finish", {"upload_id": upload_id,
                    "part_index": part["index"], "block_size": str(len(chunk)), "md5": hashlib.md5(chunk).hexdigest()})
                offset += len(chunk)
            if offset != len(image):
                raise QQError("incomplete_upload_parts")
            body["upload_id"] = upload_id
        uploaded = self._request(group_path + "/files", body)
        file_info = uploaded.get("file_info")
        if not isinstance(file_info, str) or not file_info:
            raise QQError("image_upload_ack_missing")
        sent = self._request(group_path + "/messages", {"msg_type": 7,
            "media": {"file_info": file_info}, "content": caption}, sending=True)
        if not isinstance(sent.get("id"), str) or not sent["id"]:
            raise QQError("send_ack_missing", True)
        return sent["id"]

    def send_video(self, group_openid, filename, path, caption=""):
        from .video_downloads import QQ_MAX_BYTES
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", group_openid) or group_openid == self.settings["target_group_number"]:
            raise QQError("group_not_bound")
        path = Path(path);size = path.stat().st_size
        if not 0 < size <= QQ_MAX_BYTES:raise QQError("video_too_large_for_qq")
        # QQ's >30 MB video soft limit becomes a file; keep the complete readable MP4.
        file_type = 2 if size <= 30_000_000 else 4
        group_path = "/v2/groups/" + quote(group_openid, safe="")
        body = {"file_type":file_type,"file_name":filename,"srv_send_msg":False}
        if size <= 3_500_000:
            body["file_data"] = base64.b64encode(path.read_bytes()).decode("ascii")
        else:
            md5, sha1, first_md5 = hashlib.md5(), hashlib.sha1(), hashlib.md5()
            remaining = 10002432
            with path.open("rb") as handle:
                for chunk in iter(lambda:handle.read(1024 * 1024),b""):
                    md5.update(chunk);sha1.update(chunk)
                    if remaining > 0:first_md5.update(chunk[:remaining]);remaining -= len(chunk)
            prepared = self._request(group_path + "/upload_prepare", {"file_type":file_type,"file_name":filename,
                "file_size":str(size),"md5":md5.hexdigest(),"sha1":sha1.hexdigest(),"md5_10m":first_md5.hexdigest()})
            upload_id, parts = prepared.get("upload_id"), prepared.get("parts")
            if not isinstance(upload_id,str) or not isinstance(parts,list) or not 1 <= len(parts) <= 200:
                raise QQError("invalid_video_upload_prepare")
            first_index=parts[0].get("index") if isinstance(parts[0],dict) else None
            if type(first_index) is not int or first_index not in (0,1):raise QQError("invalid_video_upload_part")
            offset = 0
            with path.open("rb") as handle:
                for index,part in enumerate(parts):
                    try:
                        part_size = int(part.get("block_size",prepared.get("block_size",0)))
                        signed_url = part["presigned_url"];url = urlsplit(signed_url);host = url.hostname or ""
                        if (part["index"] != index + first_index or not 0 < part_size <= 20 * 1024 * 1024
                            or url.scheme != "https" or url.username or url.password or url.port not in (None,443)
                            or not any(host.endswith(s) for s in (".myqcloud.com",".tencentcos.cn"))):raise ValueError()
                        chunk = handle.read(part_size)
                        if not chunk:raise ValueError()
                    except (KeyError,ValueError,TypeError,AttributeError):raise QQError("invalid_video_upload_part") from None
                    try:
                        with self.opener.open(Request(signed_url,data=chunk,method="PUT",headers={"Content-Type":"application/octet-stream"}),timeout=60) as response:response.read(1024)
                    except Exception:raise QQError("video_part_upload_failed") from None
                    self._request(group_path+"/upload_part_finish",{"upload_id":upload_id,"part_index":part["index"],
                        "block_size":str(len(chunk)),"md5":hashlib.md5(chunk).hexdigest()})
                    offset += len(chunk)
            if offset != size:raise QQError("incomplete_video_upload")
            body["upload_id"] = upload_id
        uploaded = self._request(group_path+"/files",body,timeout=60)
        info = uploaded.get("file_info")
        if not isinstance(info,str) or not info:raise QQError("video_upload_ack_missing")
        sent = self._request(group_path+"/messages",{"msg_type":7,"media":{"file_info":info},"content":caption},sending=True)
        if not isinstance(sent.get("id"),str) or not sent["id"]:raise QQError("send_ack_missing",True)
        return sent["id"]


def format_parts(post, event_type):
    label = "新帖" if event_type == "new_post" else "内容更新"
    body = post.get("text") if not post["locked"] else None
    body = body if body is not None else "有新动态，当前会员正文暂不可读，请前往原站查看。"
    chunks = [body[i:i + 1400] for i in range(0, len(body), 1400)] or [""]
    published = datetime.fromisoformat(post["createdAt"].replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Singapore")).strftime("%Y-%m-%d %H:%M（新加坡时间）")
    parts = []
    for index, chunk in enumerate(chunks, 1):
        split = f"（{index}/{len(chunks)}）" if len(chunks) > 1 else ""
        parts.append(f"TradingX {label} #{post['id']}{split}\n发布于 {published}\n\n{chunk}\n\n配图：{len(post['images'])} 张\n{post['url']}")
    return parts


class QQQueue:
    def __init__(self, store):
        self.store = store
        self.images = ImageArchive(store)
        from .video_downloads import VideoDownloads
        self.video_downloads = VideoDownloads(store)
        with store.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS qq_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
                part INTEGER NOT NULL, group_openid TEXT NOT NULL, content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL, error_code TEXT, message_id TEXT,
                UNIQUE(event_id,part,group_openid));""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(qq_outbox)")}
            if "message_type" not in columns:
                db.execute("ALTER TABLE qq_outbox ADD COLUMN message_type TEXT NOT NULL DEFAULT 'text'")
            if "asset_key" not in columns:
                db.execute("ALTER TABLE qq_outbox ADD COLUMN asset_key TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS qq_image_dedup ON qq_outbox(group_openid,asset_key) WHERE message_type='image'")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS qq_video_file_dedup ON qq_outbox(group_openid,asset_key) WHERE message_type='video_file'")

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE qq_outbox SET status='uncertain',error_code='worker_interrupted' WHERE status='sending'")

    def binding_command(self):
        config = self.store.config
        filename = config.root / "secrets" / "qq_bind_code"
        if not filename.exists():
            descriptor = os.open(filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(secrets.token_hex(8))
        settings = load_settings(config)
        return f"绑定推送 {settings['target_group_number']} {filename.read_text().strip()}"

    def bind(self, group_openid, command):
        settings = load_settings(self.store.config)
        if settings.get("group_openid"):
            return settings["group_openid"] == group_openid
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", group_openid) or group_openid == settings["target_group_number"]:
            return False
        if not secrets.compare_digest(command.strip().encode(), self.binding_command().encode()):
            return False
        # Both changes in the DB transaction ensure no history is replayed after binding.
        with self.store.connect() as db:
            cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
            self.store._update(db, qq_event_cursor=cursor, qq_group_openid=group_openid, qq_bound_at=now())
        settings.update(group_openid=group_openid, enabled=True)
        save_settings(self.store.config, settings)
        return True

    def enqueue(self):
        settings = load_settings(self.store.config)
        if not settings.get("enabled") or not settings.get("group_openid"):
            return
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store._state(db)
            if "qq_event_cursor" not in state:
                cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
                self.store._update(db, qq_event_cursor=cursor)
                return
            rows = db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 1000", (state["qq_event_cursor"],)).fetchall()
            for event in rows:
                if event["event_type"] in ("video_new", "video_updated") and event["event_type"] in settings["events"]:
                    from .videos import format_video
                    video = json.loads(event["details_json"])["video"]
                    db.execute("INSERT OR IGNORE INTO qq_outbox(event_id,part,group_openid,content,updated_at,message_type) VALUES(?,1,?,?,?,'video')",
                        (event["id"], settings["group_openid"], format_video(video, event["event_type"], event["observed_at"]), now()))
                    continue
                if event["event_type"] in settings["events"]:
                    revision = db.execute("SELECT payload_json FROM revisions WHERE post_id=? AND observed_at<=? ORDER BY id DESC LIMIT 1",
                                          (event["post_id"], event["observed_at"])).fetchone()
                    if revision:
                        post = json.loads(revision[0])
                        parts = format_parts(post, event["event_type"])
                        for number, part in enumerate(parts, 1):
                            db.execute("INSERT OR IGNORE INTO qq_outbox(event_id,part,group_openid,content,updated_at) VALUES(?,?,?,?,?)",
                                       (event["id"], number, settings["group_openid"], part, now()))
                        self._add_images(db, settings, event["id"], post, len(parts))
            if rows:
                self.store._update(db, qq_event_cursor=rows[-1]["id"])
            if settings.get("send_video_files",False):
                # Reconcile completed files even if a previous worker already consumed their text events.
                files = db.execute("SELECT j.event_id,j.video_json,j.asset_key,f.size FROM video_download_jobs j JOIN video_files f ON f.asset_key=j.asset_key "
                    "WHERE j.status='ready' AND NOT EXISTS(SELECT 1 FROM qq_outbox o WHERE o.asset_key=j.asset_key AND o.group_openid=? AND o.message_type='video_file') ORDER BY j.id LIMIT 100",(settings["group_openid"],)).fetchall()
                for file in files:
                    video = json.loads(file["video_json"])
                    caption = f"第{video['number']}期" if video["number"] else f"视频{video['id']}"
                    caption += f" · {video['title']} · 完整MP4（{file['size']/1000000:.1f} MB）"
                    db.execute("INSERT OR IGNORE INTO qq_outbox(event_id,part,group_openid,content,updated_at,message_type,asset_key) VALUES(?,2,?,?,?,'video_file',?)",
                        (file["event_id"],settings["group_openid"],caption,now(),file["asset_key"]))

    def _add_images(self, db, settings, event_id, post, last_part):
        if not settings.get("send_images", True) or post["locked"]:
            return 0
        added = 0
        for index, image in enumerate(post["images"], 1):
            try:
                key = self.images.register(post, image, db)
            except FetchError as error:
                self.store._update(db, qq_image_error=error.code)
                continue
            if not key:
                continue
            cursor = db.execute("INSERT OR IGNORE INTO qq_outbox(event_id,part,group_openid,content,updated_at,message_type,asset_key) VALUES(?,?,?,?,?,'image',?)",
                (event_id, last_part + index, settings["group_openid"], f"TradingX #{post['id']} 配图 {index}/{len(post['images'])}", now(), key))
            added += cursor.rowcount
        return added

    def enqueue_images(self, post_id):
        settings = load_settings(self.store.config)
        if not settings.get("group_openid") or not settings.get("enabled"):
            raise QQError("group_not_bound")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT current_json FROM posts WHERE id=?", (post_id,)).fetchone()
            event = db.execute("SELECT id FROM events WHERE post_id=? ORDER BY id DESC LIMIT 1", (post_id,)).fetchone()
            if not row or not event:
                raise QQError("post_event_missing")
            last_part = db.execute("SELECT COALESCE(MAX(part),0) FROM qq_outbox WHERE event_id=? AND group_openid=?", (event[0], settings["group_openid"])).fetchone()[0]
            return self._add_images(db, settings, event[0], json.loads(row[0]), last_part)

    def deliver_one(self, api):
        settings = load_settings(self.store.config)
        if not settings.get("enabled") or not settings.get("group_openid"):
            return False
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM qq_outbox WHERE status IN ('uncertain','sending') OR (status='failed' AND message_type!='video_file') LIMIT 1").fetchone():
                return False
            row = db.execute("SELECT * FROM qq_outbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return False
            if row["group_openid"] != settings["group_openid"]:
                raise QQError("target_changed")
            asset = self.images.read(row["asset_key"]) if row["message_type"] == "image" else None
            if row["message_type"] == "image" and not asset:
                self.store._update(db, qq_waiting_image=True)
                return False
            video_asset = self.video_downloads.asset(row["asset_key"]) if row["message_type"] == "video_file" else None
            if row["message_type"] == "video_file" and not video_asset:
                db.execute("UPDATE qq_outbox SET status='failed',error_code='video_file_missing',updated_at=? WHERE id=?",(now(),row["id"]))
                return False
            db.execute("UPDATE qq_outbox SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?", (now(), row["id"]))
        try:
            if row["message_type"] == "image":
                message_id = api.send_image(row["group_openid"], asset[2], asset[0], row["content"])
            elif row["message_type"] == "video_file":
                from .video_downloads import sha256_file
                if sha256_file(video_asset["path"]) != video_asset["sha256"]:raise QQError("video_file_checksum_failed")
                filename = video_asset["display_name"]
                message_id = api.send_video(row["group_openid"],filename,video_asset["path"],row["content"])
            else:
                message_id = api.send(row["group_openid"], row["content"])
        except QQError as error:
            with self.store.connect() as db:
                db.execute("UPDATE qq_outbox SET status=?,error_code=?,updated_at=? WHERE id=?",
                           ("uncertain" if error.uncertain else "failed", error.code, now(), row["id"]))
                self.store._update(db, qq_last_error=error.code, qq_delivery_paused=True)
            return False
        with self.store.connect() as db:
            db.execute("UPDATE qq_outbox SET status='sent',message_id=?,updated_at=? WHERE id=?", (message_id, now(), row["id"]))
            self.store._update(db, qq_last_error=None, qq_last_test_error=None, qq_delivery_paused=False, qq_waiting_image=False, qq_last_sent=now())
        return True

    def send_test(self, api):
        settings = load_settings(self.store.config)
        if not settings.get("group_openid"):
            raise QQError("group_not_bound")
        try:
            message_id = api.send(settings["group_openid"],
                "订阅更新提醒测试：采集与 QQ 发送服务已连接。\n数据来源：https://wall-street-skill.com/tradingx\n后续将自动发送新帖和编辑更新。")
        except QQError as error:
            with self.store.connect() as db:
                self.store._update(db, qq_last_test_error=error.code, qq_last_test_attempt=now())
            raise
        with self.store.connect() as db:
            self.store._update(db, qq_last_test=now(), qq_last_test_error=None, qq_last_test_attempt=now())
        return message_id

    def send_video_test(self, api):
        from .videos import VideoMonitor, format_video
        settings = load_settings(self.store.config)
        if not settings.get("group_openid"):
            raise QQError("group_not_bound")
        videos = VideoMonitor(self.store).listing(1)
        if not videos:
            raise QQError("video_baseline_missing")
        try:
            message_id = api.send(settings["group_openid"], format_video(videos[0], test=True))
        except QQError as error:
            with self.store.connect() as db:
                self.store._update(db, qq_last_test_error=error.code)
            raise
        with self.store.connect() as db:
            self.store._update(db, qq_video_last_test=now(), qq_last_test_error=None)
        return message_id

    def status(self):
        settings = load_settings(self.store.config)
        with self.store.connect() as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT status,COUNT(*) FROM qq_outbox GROUP BY status")}
            sent = {row[0]: row[1] for row in db.execute("SELECT message_type,COUNT(*) FROM qq_outbox WHERE status='sent' GROUP BY message_type")}
            file_counts = {row[0]:row[1] for row in db.execute("SELECT status,COUNT(*) FROM qq_outbox WHERE message_type='video_file' GROUP BY status")}
            file_error = db.execute("SELECT error_code FROM qq_outbox WHERE message_type='video_file' AND status IN ('failed','uncertain') ORDER BY id DESC LIMIT 1").fetchone()
            state = self.store._state(db)
        result = {"appid": settings["appid"], "target_group_name": settings["target_group_name"],
                  "target_group_number": settings["target_group_number"], "bound": bool(settings["group_openid"]),
                  "enabled": settings["enabled"], "secret_configured": (self.store.config.root / "secrets" / "qq_app_secret").exists(),
                  "outbox": counts, "sent_text": sent.get("text", 0), "sent_images": sent.get("image", 0),
                  "sent_videos": sent.get("video", 0), "video_notifications_enabled": "video_new" in settings["events"],
                  "sent_video_files":sent.get("video_file",0),"video_file_outbox":file_counts,"video_file_error":file_error[0] if file_error else None,
                  "video_last_test": state.get("qq_video_last_test"),
                  "images_enabled": settings.get("send_images", True), "waiting_image": state.get("qq_waiting_image", False),
                  "last_sent": state.get("qq_last_sent"),
                  "last_test": state.get("qq_last_test"),
                  "last_error": state.get("qq_last_error") or state.get("qq_last_test_error"),
                  "active_send_verified": bool(state.get("qq_last_test") or state.get("qq_last_sent")) and not state.get("qq_last_test_error"),
                  "connection": state.get("qq_connection", "not_started"), "last_heartbeat": state.get("qq_heartbeat")}
        if not result["bound"]:
            result["binding_command"] = self.binding_command()
        return result
