from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
import random
import signal
import threading
import time

from .client import Client, FetchError
from .store import now


LOG = logging.getLogger("tradingx")


def setup_logging(config):
    handler = RotatingFileHandler(config.root / "logs" / "monitor.log", maxBytes=2_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.setLevel(logging.INFO)
    LOG.handlers.clear()
    LOG.addHandler(handler)
    LOG.addHandler(logging.StreamHandler())


@contextmanager
def lock(config, name):
    with (config.root / "data" / name).open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FetchError("already_running", "已有进程正在执行此操作，请勿重复启动") from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def collect(config, store, client=None):
    started = now()
    try:
        with lock(config, "poll.lock"):
            snapshot = (client or Client(config)).fetch()
            result = store.success(snapshot, started)
            from .images import ImageArchive
            result["images"] = ImageArchive(store).sync(snapshot["posts"])
        LOG.info("poll_ok posts=%s new=%s changed=%s viewer=%s baseline=%s",
                 result["posts"], result["new"], result["changed"], result["viewer"], result["baseline"])
        return result, None
    except FetchError as error:
        # A simultaneous manual 'once' must not mark the active daemon as failed.
        if error.code != "already_running":
            store.failure(error, started)
        LOG.warning("poll_failed code=%s message=%s", error.code, str(error))
        return None, error


def next_delay(config, failures, error=None):
    if failures:
        delay = min(config.max_backoff_seconds, config.interval_seconds * 2 ** min(failures, 12))
        return max(delay, error.retry_after if error else 0)
    return max(60, config.interval_seconds * random.uniform(1 - config.jitter_ratio, 1 + config.jitter_ratio))


def run(config, store, no_dashboard=False):
    from .web import make_server
    from .videos import VideoMonitor
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    with lock(config, "run.lock"):
        server = None
        try:
            videos = VideoMonitor(store)
            if not no_dashboard:
                server = make_server(config, store)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                LOG.info("dashboard http://127.0.0.1:%s", config.port)
            next_post = time.time()
            pending = store.status().get("next_poll_at")
            if pending:
                try:
                    wait_until = datetime.fromisoformat(pending.replace("Z", "+00:00"))
                    next_post = wait_until.timestamp()
                except (ValueError, TypeError):
                    pass
            next_video = videos.status().get("next_check") or time.time()
            while not stopped.is_set():
                if time.time() >= next_post:
                    try:
                        _, error = collect(config, store)
                    except Exception:
                        # Never include arbitrary exception strings or remote bodies in logs.
                        error = FetchError("internal_error", "内部错误，请检查磁盘空间、数据库和程序版本")
                        store.failure(error, now())
                        LOG.error("poll_failed code=internal_error")
                    failures = store.status().get("consecutive_failures", 0)
                    next_post = time.time() + next_delay(config, failures, error)
                    store.schedule(datetime.fromtimestamp(next_post, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
                if not stopped.is_set() and time.time() >= next_video:
                    _, video_error = videos.poll()
                    if video_error:
                        LOG.warning("video_poll_failed code=%s", video_error.code)
                    next_video = max(time.time() + 1, videos.status().get("next_check") or time.time() + 10)
                stopped.wait(max(0, min(next_post, next_video) - time.time()))
        finally:
            if server:
                server.shutdown()
                server.server_close()
            store.schedule(None)
            LOG.info("monitor_stopped")
