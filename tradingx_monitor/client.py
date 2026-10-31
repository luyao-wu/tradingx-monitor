"""Use only the same read endpoint and Bearer scheme used by the site's UI."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import SOURCE


class FetchError(Exception):
    def __init__(self, code, message, retry_after=0):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FetchError("redirect_blocked", "数据接口发生重定向，已停止请求以保护凭据")


def read_token(config):
    value = os.environ.get("TRADINGX_TOKEN")
    if value is None and config.token_file.exists():
        if config.token_file.stat().st_mode & 0o077:
            raise FetchError("token_permissions", "凭据文件权限过宽，请运行 auth set 重新保存")
        value = config.token_file.read_text().strip()
    value = (value or "").strip()
    if value.startswith("Bearer "):
        value = value[7:].strip()
    if any(c.isspace() for c in value) or len(value) > 16384:
        raise FetchError("invalid_token", "凭据格式不正确，请重新配置；不要粘贴整条请求")
    return value


def retry_after_seconds(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 0


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise FetchError("schema_changed", f"接口字段 {name} 格式变化，需要检查适配器")
    return value


def _timestamp(value, name, nullable=False):
    if nullable and value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
    except (AttributeError, TypeError, ValueError):
        raise FetchError("schema_changed", f"接口字段 {name} 不是有效时间") from None
    return value


def normalize_post(post):
    if not isinstance(post, dict) or type(post.get("locked")) is not bool:
        raise FetchError("schema_changed", "接口缺少有效的 locked 字段，停止解析以免误存正文")
    locked = post["locked"]
    text = None if locked else post.get("text")
    if not locked and not isinstance(text, str):
        raise FetchError("schema_changed", "可读帖子缺少 text 字段")
    images = post.get("images", [])
    if not isinstance(images, list) or any(not isinstance(i, dict) for i in images):
        raise FetchError("schema_changed", "接口 images 字段格式变化")
    clean_images = []
    for image in images:
        clean = {key: image[key] for key in ("id", "thumb", "url") if isinstance(image.get(key), str)}
        if locked:
            clean.pop("url", None)
        clean_images.append(clean)
    post_id = _integer(post.get("id"), "id", 1)
    return {
        "id": post_id,
        "createdAt": _timestamp(post.get("createdAt"), "createdAt"),
        "editedAt": _timestamp(post.get("editedAt"), "editedAt", nullable=True),
        "likes": _integer(post.get("likes", 0), "likes"),
        "views": _integer(post.get("views", 0), "views"),
        "locked": locked,
        "text": text,
        "textLength": _integer(post.get("textLength", len(text or "")), "textLength"),
        "images": clean_images,
        "url": f"{SOURCE}/tradingx/{post_id}",
    }


class Client:
    def __init__(self, config, opener=None):
        self.config = config
        self.opener = opener or build_opener(NoRedirect())

    def _get(self, url, token):
        headers = {"Accept": "application/json", "User-Agent": "TradingX-Local-Monitor/1.0", "Cache-Control": "no-cache"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with self.opener.open(Request(url, headers=headers), timeout=self.config.request_timeout) as response:
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise FetchError("response_too_large", "接口响应超过 8 MiB，需人工检查")
        except HTTPError as error:
            delay = retry_after_seconds(error.headers.get("Retry-After"))
            if error.code in (401, 403):
                raise FetchError("auth_required", "登录态失效或无权限，请在本机更新会员凭据", delay) from None
            if error.code == 429:
                raise FetchError("rate_limited", "网站限制请求频率，已延后重试", delay) from None
            raise FetchError(f"http_{error.code}", f"网站接口返回 HTTP {error.code}", delay) from None
        except (URLError, TimeoutError, OSError):
            raise FetchError("network_error", "无法连接网站或请求超时", 0) from None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise FetchError("invalid_json", "接口没有返回有效 JSON，可能是临时故障或验证页面") from None
        if not isinstance(data, dict) or data.get("ok") is False or data.get("error"):
            raise FetchError("server_rejected", "网站拒绝本次读取；请检查网站状态和会员权限")
        return data

    def fetch(self):
        token = read_token(self.config)
        all_posts, seen = [], set()
        before = None
        viewer, total = None, None
        for page in range(1, self.config.max_pages + 1):
            url = f"{SOURCE}/api/tradingx-posts"
            if before is not None:
                url += "?" + urlencode({"before": before})
            data = self._get(url, token)
            if not isinstance(data.get("posts"), list) or type(data.get("hasMore")) is not bool:
                raise FetchError("schema_changed", "接口 posts/hasMore 字段变化，需要检查适配器")
            page_viewer = data.get("viewer")
            if not isinstance(page_viewer, str) or not page_viewer or len(page_viewer) > 40:
                raise FetchError("schema_changed", "接口 viewer 字段变化")
            if viewer is not None and viewer != page_viewer:
                raise FetchError("access_changed_mid_fetch", "分页期间会员权限变化，下轮重新读取")
            viewer = page_viewer
            if total is None and data.get("total") is not None:
                total = _integer(data["total"], "total")
            posts = [normalize_post(p) for p in data["posts"]]
            ids = [p["id"] for p in posts]
            if ids != sorted(set(ids), reverse=True) or seen.intersection(ids) or (before is not None and any(i >= before for i in ids)):
                raise FetchError("pagination_changed", "分页编号没有递减或发生重复，已停止本轮")
            all_posts.extend(posts)
            seen.update(ids)
            if not data["hasMore"]:
                return {"posts": all_posts, "viewer": viewer, "total": total, "pages": page, "truncated": False, "auth_configured": bool(token)}
            if not posts:
                raise FetchError("pagination_changed", "接口返回空分页但仍声称有下一页")
            before = ids[-1]
        return {"posts": all_posts, "viewer": viewer, "total": total, "pages": page, "truncated": True, "auth_configured": bool(token)}
