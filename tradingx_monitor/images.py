"""Archive authorized original images; never forward member credentials off-origin."""
import hashlib
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, build_opener

from .client import FetchError, NoRedirect, read_token, retry_after_seconds
from .config import SOURCE
from .store import now


MAX_IMAGE_BYTES = 20 * 1024 * 1024


def original_url(post_id, image):
    value = image.get("url")
    if not isinstance(value, str) or not value:
        return None
    url = urljoin(SOURCE, value)
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    if (parts.scheme != "https" or parts.netloc != urlsplit(SOURCE).netloc
            or parts.path != "/api/tradingx-images" or parts.fragment
            or set(query) != {"post", "id"}
            or query["post"] != [str(post_id)] or query["id"] != [image.get("id")]):
        raise FetchError("image_origin_blocked", "原图地址不符合原站图片接口，已停止请求")
    return url


def image_format(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise FetchError("invalid_image", "原图响应不是受支持的图片文件")


class ImageArchive:
    def __init__(self, store, opener=None):
        self.store = store
        self.config = store.config
        self.folder = self.config.root / "data" / "images"
        self.folder.mkdir(exist_ok=True, mode=0o700)
        self.folder.chmod(0o700)
        self.opener = opener or build_opener(NoRedirect())
        with store.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS image_assets (
                asset_key TEXT PRIMARY KEY, post_id INTEGER NOT NULL, image_id TEXT NOT NULL,
                source_url TEXT NOT NULL, filename TEXT, content_type TEXT, size INTEGER,
                sha256 TEXT, status TEXT NOT NULL DEFAULT 'pending', error_code TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS image_assets_post ON image_assets(post_id);
            """)

    def register(self, post, image, db=None):
        if post["locked"]:
            return None
        url = original_url(post["id"], image)
        if url is None:
            return None
        key = hashlib.sha256(url.encode()).hexdigest()
        values = (key, post["id"], image["id"], url, now())
        sql = "INSERT OR IGNORE INTO image_assets(asset_key,post_id,image_id,source_url,updated_at) VALUES(?,?,?,?,?)"
        if db is not None:
            db.execute(sql, values)
        else:
            with self.store.connect() as connection:
                connection.execute(sql, values)
        return key

    def row(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            return None
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM image_assets WHERE asset_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def read(self, key):
        row = self.row(key)
        if not row or row["status"] != "ready":
            return None
        if not re.fullmatch(re.escape(key) + r"\.(jpg|png|gif|webp)", row["filename"] or ""):
            return None
        try:
            data = (self.folder / row["filename"]).read_bytes()
        except OSError:
            return None
        if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            return None
        return data, row["content_type"], row["filename"]

    def download(self, key):
        row = self.row(key)
        if not row:
            raise FetchError("image_missing", "图片归档记录不存在")
        if self.read(key):
            return "cached"
        if row["retry_at"] > time.time():
            return "deferred"
        temporary = self.folder / (key + ".tmp")
        try:
            # Validate again before attaching the member token to a stored URL.
            url = original_url(row["post_id"], {"id": row["image_id"], "url": row["source_url"]})
            token = read_token(self.config)
            if not token:
                raise FetchError("image_auth_required", "下载原图需要本机会员登录态")
            request = Request(url, headers={"Authorization": "Bearer " + token,
                "Accept": "image/*", "User-Agent": "TradingX-Local-Monitor/1.0"})
            with self.opener.open(request, timeout=self.config.request_timeout) as response:
                data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise FetchError("image_too_large", "单张原图超过 20 MiB，未保存")
            extension, mime = image_format(data)
            filename = key + "." + extension
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary, self.folder / filename)
            with self.store.connect() as db:
                db.execute("UPDATE image_assets SET filename=?,content_type=?,size=?,sha256=?,status='ready',"
                           "error_code=NULL,attempts=attempts+1,retry_at=0,updated_at=? WHERE asset_key=?",
                           (filename, mime, len(data), hashlib.sha256(data).hexdigest(), now(), key))
            return "downloaded"
        except HTTPError as error:
            code = "image_auth_required" if error.code in (401, 403) else "image_http_" + str(error.code)
            failure = FetchError(code, "原图下载被原站拒绝", retry_after_seconds(error.headers.get("Retry-After")))
        except (URLError, TimeoutError, OSError):
            failure = FetchError("image_network_error", "原图下载失败，稍后重试")
        except FetchError as error:
            failure = error
        finally:
            temporary.unlink(missing_ok=True)
        delay = max(failure.retry_after, min(3600, 120 * 2 ** min(row["attempts"], 5)))
        with self.store.connect() as db:
            db.execute("UPDATE image_assets SET status='failed',error_code=?,attempts=attempts+1,retry_at=?,updated_at=? WHERE asset_key=?",
                       (failure.code, time.time() + delay, now(), key))
        return "failed"

    def sync(self, posts, limit=20):
        keys = []
        for post in posts:
            for image in post["images"]:
                try:
                    key = self.register(post, image)
                except FetchError:
                    continue
                if key and key not in keys:
                    keys.append(key)
        results = {"downloaded": 0, "cached": 0, "failed": 0, "deferred": 0}
        attempts = 0
        for key in keys:
            row = self.row(key)
            if row["status"] == "ready" and self.read(key):
                results["cached"] += 1
                continue
            if attempts >= limit:
                results["deferred"] += 1
                continue
            result = self.download(key)
            results[result] += 1
            if result not in ("cached", "deferred"):
                attempts += 1
        return results

    def decorate(self, posts):
        for post in posts:
            if post["locked"]:
                continue
            with self.store.connect() as db:
                rows = {r["image_id"]: dict(r) for r in db.execute("SELECT * FROM image_assets WHERE post_id=?", (post["id"],))}
            for image in post["images"]:
                row = rows.get(image.get("id"))
                image["downloadStatus"] = row["status"] if row else "pending"
                if row and row["status"] == "ready" and (self.folder / row["filename"]).is_file():
                    image["localUrl"] = "/api/images/" + row["asset_key"]
                    image["size"] = row["size"]
                elif row:
                    image["downloadError"] = row["error_code"]
        return posts

    def status(self):
        with self.store.connect() as db:
            counts = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM image_assets GROUP BY status")}
            size = db.execute("SELECT COALESCE(SUM(size),0) FROM image_assets WHERE status='ready'").fetchone()[0]
        return {"counts": counts, "total": sum(counts.values()), "bytes": size, "limit_bytes": MAX_IMAGE_BYTES}
