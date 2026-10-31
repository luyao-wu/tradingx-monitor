"""Explicit account login; passwords are used once and never persisted."""
import json
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from .client import FetchError, NoRedirect, retry_after_seconds
from .config import SOURCE


def save_token(config, value):
    if not isinstance(value, str) or not value or value in ("null", "undefined") or any(c.isspace() for c in value) or len(value) > 16384:
        raise FetchError("invalid_token", "登录凭据格式不正确，未修改已有配置")
    descriptor, filename = tempfile.mkstemp(dir=config.token_file.parent, prefix=".token-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(filename, config.token_file)
    finally:
        Path(filename).unlink(missing_ok=True)


def login(config, email, password, opener=None):
    if not isinstance(email, str) or not isinstance(password, str):
        raise FetchError("invalid_credentials", "请输入登录邮箱和密码")
    email = email.strip()
    if not email or "@" not in email or len(email) > 254 or not password or len(password) > 512:
        raise FetchError("invalid_credentials", "请输入有效的登录邮箱和密码")
    request = Request(f"{SOURCE}/api/login",
                      data=json.dumps({"method": "password", "email": email, "password": password}).encode(),
                      headers={"Content-Type": "application/json", "Accept": "application/json",
                               "User-Agent": "TradingX-Local-Monitor/1.0"}, method="POST")
    try:
        with (opener or build_opener(NoRedirect())).open(request, timeout=config.request_timeout) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise FetchError("login_response_invalid", "原站登录响应异常，请在原站检查登录状态")
    except HTTPError as error:
        if error.code == 429:
            raise FetchError("login_rate_limited", "原站限制登录频率，请稍后再试", retry_after_seconds(error.headers.get("Retry-After"))) from None
        if error.code == 403:
            raise FetchError("login_verification_required", "原站拒绝登录；如需验证，请手动在原站完成后使用令牌接入") from None
        raise FetchError("login_failed", "登录失败，请检查邮箱和密码；没有保存密码或替换原有令牌") from None
    except (URLError, TimeoutError, OSError):
        raise FetchError("login_network_error", "连接原站超时或失败，请稍后再试") from None
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise FetchError("login_response_invalid", "原站没有返回有效登录结果，请在原站检查是否需要验证") from None
    if not isinstance(data, dict) or data.get("ok") is False or data.get("error") or not isinstance(data.get("token"), str):
        raise FetchError("login_failed", "登录未成功，请检查邮箱、密码或原站验证要求")
    return data["token"]


def connect_account(config, store, email, password):
    from .runner import collect
    if os.environ.get("TRADINGX_TOKEN"):
        raise FetchError("environment_token_active", "后台正在使用环境变量凭据，请清除该环境变量并重启后台后再接入账号")
    token = login(config, email, password)
    save_token(config, token)
    next_poll = store.status().get("next_poll_at")
    result, error = collect(config, store)
    if next_poll:
        store.schedule(next_poll)
    if error:
        return {"connected": True, "verified": False,
                "message": "登录令牌已保存，密码未保存。正文验证暂未完成，下一轮将自动重试。", "code": error.code}
    state = store.status()
    readable = state["last_post_count"] - state["last_locked_count"]
    full = state["viewer"] in ("member", "owner") and state["last_locked_count"] == 0
    return {"connected": True, "verified": full, "viewer": state["viewer"], "readable_posts": readable,
            "message": f"已接入并归档 {readable} 条可读正文，密码未保存。" if full else "登录已接入，但当前账号仍有正文受限，请检查会员状态。密码未保存。"}
