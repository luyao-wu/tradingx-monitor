from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
import re
from urllib.parse import parse_qs, quote, urlsplit


ASSETS = Path(__file__).parent / "static"


def make_server(config, store):
    from .images import ImageArchive
    from .videos import VideoMonitor
    from .video_downloads import VideoDownloads
    images = ImageArchive(store)
    videos = VideoMonitor(store)
    downloads = VideoDownloads(store)
    csrf = secrets.token_urlsafe(32)
    login_lock = threading.Lock()
    last_login = [None]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body, content_type="application/json; charset=utf-8", status=200, cache="no-store"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host", "") not in hosts:
                self.reply({"error": "仅允许本机访问"}, status=403)
                return
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            try:
                limit = min(1000, max(1, int(query.get("limit", ["50"])[0])))
                after = max(0, int(query["after"][0])) if "after" in query else None
                if parsed.path in ("/", "/app.js", "/style.css"):
                    filename, mime = {
                        "/": ("index.html", "text/html; charset=utf-8"),
                        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                        "/style.css": ("style.css", "text/css; charset=utf-8"),
                    }[parsed.path]
                    self.reply((ASSETS / filename).read_bytes(), mime)
                elif parsed.path == "/api/status":
                    self.reply(store.status())
                elif parsed.path == "/api/qq/status":
                    if not (config.root / "qq_bot.json").exists():
                        self.reply({"configured": False})
                    else:
                        from .qq_push import QQQueue
                        self.reply({"configured": True, **QQQueue(store).status()})
                elif parsed.path == "/api/auth/session":
                    self.reply({"csrf": csrf})
                elif parsed.path == "/api/posts":
                    self.reply({"posts": images.decorate(store.posts(limit))})
                elif parsed.path == "/api/images/status":
                    self.reply(images.status())
                elif parsed.path == "/api/videos/status":
                    self.reply({**videos.status(),"downloads":downloads.status()})
                elif parsed.path == "/api/videos":
                    self.reply({"videos": videos.listing(limit)})
                elif parsed.path.startswith("/api/video-files/"):
                    asset=downloads.asset(parsed.path[len("/api/video-files/"):])
                    if not asset:self.reply({"error":"视频尚未下载"},status=404)
                    else:self.video_file(asset)
                elif parsed.path.startswith("/api/images/"):
                    asset = images.read(parsed.path[len("/api/images/"):])
                    if asset:
                        self.reply(asset[0], asset[1], cache="private, max-age=3600")
                    else:
                        self.reply({"error": "原图尚未下载或校验失败"}, status=404)
                elif parsed.path == "/api/events":
                    self.reply({"events": store.events(limit, after)})
                elif parsed.path == "/api/revisions":
                    self.reply({"revisions": store.revisions(int(query.get("id", ["0"])[0]), limit)})
                elif parsed.path == "/healthz":
                    state = store.status()
                    self.reply(state, status=200 if state["healthy"] else 503)
                else:
                    self.reply({"error": "不存在的路径"}, status=404)
            except (ValueError, IndexError):
                self.reply({"error": "参数格式不正确"}, status=400)
            except Exception:
                self.reply({"error": "本地读取失败，请检查数据库和日志"}, status=500)

        def video_file(self,asset):
            size=asset["size"];start,end=0,size-1
            requested=self.headers.get("Range")
            if requested:
                match=re.fullmatch(r"bytes=(\d*)-(\d*)",requested)
                try:
                    if not match or not any(match.groups()):raise ValueError()
                    if match[1]:start=int(match[1]);end=min(int(match[2]),end) if match[2] else end
                    else:
                        suffix=int(match[2])
                        if suffix<=0:raise ValueError()
                        start=max(0,size-suffix)
                    if start>end or start>=size:raise ValueError()
                except ValueError:
                    self.send_response(416);self.send_header("Content-Range",f"bytes */{size}");self.send_header("Content-Length","0");self.end_headers();return
            with asset["path"].open("rb") as handle:
                self.send_response(206 if requested else 200)
                self.send_header("Content-Type","video/mp4");self.send_header("Accept-Ranges","bytes")
                self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quote(asset["display_name"],safe=""))
                self.send_header("Content-Length",str(end-start+1));self.send_header("Cache-Control","private, max-age=3600")
                self.send_header("X-Content-Type-Options","nosniff");self.send_header("X-Frame-Options","DENY")
                if requested:self.send_header("Content-Range",f"bytes {start}-{end}/{size}")
                self.end_headers();handle.seek(start);remaining=end-start+1
                try:
                    while remaining:
                        chunk=handle.read(min(65536,remaining))
                        if not chunk:break
                        self.wfile.write(chunk);remaining-=len(chunk)
                except (BrokenPipeError,ConnectionResetError):pass

        def do_POST(self):
            host = self.headers.get("Host", "")
            allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if host not in allowed_hosts or self.headers.get("Origin") != f"http://{host}":
                self.reply({"error": "仅允许从本机监控台提交"}, status=403)
                return
            if self.path != "/api/auth/login":
                self.reply({"error": "不存在的路径"}, status=404)
                return
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf):
                self.reply({"error": "页面已过期，请刷新后再试"}, status=403)
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.reply({"error": "请求格式不正确"}, status=415)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
            if size <= 0 or size > 8192:
                self.reply({"error": "请求长度不正确"}, status=413)
                return
            if not login_lock.acquire(blocking=False):
                self.reply({"error": "登录正在处理中，请等待"}, status=409)
                return
            credentials = {}
            try:
                if last_login[0] is not None and time.monotonic() - last_login[0] < 5:
                    self.reply({"error": "请等待 5 秒后再试"}, status=429)
                    return
                self.connection.settimeout(config.request_timeout + 5)
                credentials = json.loads(self.rfile.read(size))
                if not isinstance(credentials, dict):
                    raise ValueError()
                last_login[0] = time.monotonic()
                from .auth import connect_account
                from .client import FetchError
                try:
                    self.reply(connect_account(config, store, credentials.get("email"), credentials.get("password")))
                except FetchError as error:
                    self.reply({"error": str(error), "code": error.code}, status=400)
            except (ValueError, UnicodeDecodeError, TimeoutError):
                self.reply({"error": "请求格式不正确或已超时"}, status=400)
            except Exception:
                self.reply({"error": "接入失败，请检查本机文件权限和服务状态"}, status=500)
            finally:
                if isinstance(credentials, dict):
                    credentials.clear()
                login_lock.release()

    server = ThreadingHTTPServer(("127.0.0.1", config.port), Handler)
    server.daemon_threads = True
    return server
