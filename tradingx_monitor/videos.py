"""Watch the same public course/video catalog used by the site's frontend."""
from datetime import datetime, timezone
import json
import re
import time
from zoneinfo import ZoneInfo

from .client import Client, FetchError, read_token
from .config import SOURCE
from .store import encode, now


def text_field(item, key, maximum=600):
    value = item.get(key, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise FetchError("video_schema_changed", "视频目录字段格式变化")
    return value


class VideoClient:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or Client(config)

    def fetch(self):
        token = read_token(self.config)
        catalog = self.client._get(SOURCE + "/api/course-items", token)
        streams = self.client._get(SOURCE + "/api/video-stream", token)
        courses, mappings = catalog.get("courses"), streams.get("episodes")
        if not isinstance(courses, list) or not isinstance(mappings, list):
            raise FetchError("video_schema_changed", "视频目录或播放列表格式变化")
        stream_ids = set()
        for item in mappings:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 1:
                raise FetchError("video_schema_changed", "视频播放列表编号格式变化")
            stream_ids.add(item["id"])
        seen, result = set(), []
        for item in courses:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 1 or item["id"] in seen:
                raise FetchError("video_schema_changed", "视频目录编号无效或重复")
            seen.add(item["id"])
            if item.get("status", "published") != "published":
                continue
            kind = text_field(item, "contentType", 32) or "video"
            title = text_field(item, "title", 500).strip()
            if not title or kind not in ("video", "article"):
                raise FetchError("video_schema_changed", "视频目录标题或类型无效")
            youtube = text_field(item, "youtubeId", 128)
            has_stream = item.get("hasStreamVideo", False)
            if type(has_stream) is not bool:
                raise FetchError("video_schema_changed", "视频可播放标记格式变化")
            if not (youtube or has_stream or item["id"] in stream_ids):
                continue
            number = item.get("number", 0)
            if type(number) is not int or number < 0:
                raise FetchError("video_schema_changed", "视频期数格式变化")
            description = text_field(item, "description", 4000)
            published_date = None
            match = re.search(r"(?:^|\s)(\d{2}|\d{4})年(\d{1,2})月(\d{1,2})日", description)
            if match:
                try:
                    year = int(match[1]);year = year + 2000 if year < 100 else year
                    published_date = datetime(year, int(match[2]), int(match[3])).date().isoformat()
                except ValueError:
                    pass
            result.append({"id": item["id"], "number": number, "title": title,
                "description": description, "category": text_field(item, "category", 64),
                "content_type": kind, "duration": text_field(item, "duration", 32),
                "youtube_id": youtube, "has_stream": has_stream or item["id"] in stream_ids,
                "published_date": published_date, "created_at": text_field(item, "createdAt", 64),
                "updated_at": text_field(item, "updatedAt", 64),
                "url": f"{SOURCE}/{'article' if kind == 'article' else 'video'}/{item['id']}"})
        return result


def meaningful(video):
    # Counts, access levels, sort order and administrative revision timestamps are not video updates.
    return {key: video[key] for key in ("title", "number", "description", "category",
        "content_type", "duration", "youtube_id", "has_stream", "url")}


def format_video(video, event_type="video_new", observed_at=None, test=False):
    section = "随机K线复盘" if video["category"] == "kline" else "课程配套视频" if video["content_type"] == "article" else "视频课程"
    label = "视频通知测试（现有视频）" if test else "新视频上线" if event_type == "video_new" else "视频信息更新"
    episode = f"第 {video['number']} 期 · " if video["number"] else ""
    detected = datetime.fromisoformat((observed_at or now()).replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Singapore")).strftime("%Y-%m-%d %H:%M")
    lines = [f"【{label}】", f"{episode}{video['title']}", f"栏目：{section}"]
    if video["duration"]:
        lines.append("时长：" + video["duration"])
    if video.get("published_date"):
        lines.append("目录日期：" + video["published_date"])
    lines.extend([f"检测时间：{detected}（新加坡时间）", "观看：" + video["url"]])
    return "\n".join(lines)


class VideoMonitor:
    def __init__(self, store):
        self.store = store
        self.config = store.config
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS videos(id INTEGER PRIMARY KEY, current_json TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)")

    def apply(self, videos):
        when = now()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store._state(db)
            baseline = not state.get("video_initialized", False)
            created = updated = 0
            for video in videos:
                row = db.execute("SELECT current_json FROM videos WHERE id=?", (video["id"],)).fetchone()
                kind = None
                if row is None:
                    db.execute("INSERT INTO videos VALUES(?,?,?,?)", (video["id"], encode(video), when, when))
                    if not baseline:
                        kind = "video_new";created += 1
                else:
                    if meaningful(json.loads(row[0])) != meaningful(video):
                        kind = "video_updated";updated += 1
                    db.execute("UPDATE videos SET current_json=?,last_seen=? WHERE id=?", (encode(video), when, video["id"]))
                if kind:
                    self.store._event(db, when, kind, video=video)
            if baseline:
                self.store._event(db, when, "video_baseline", count=len(videos))
            self.store._update(db, video_initialized=True, video_last_success=when,
                video_last_error=None, video_error_code=None, video_failures=0,
                video_current_count=len(videos), video_last_new=created, video_last_updated=updated)
        return {"baseline": baseline, "videos": len(videos), "new": created, "updated": updated}

    def poll(self, client=None):
        from .runner import lock, next_delay
        started = now()
        error = None
        result = None
        try:
            with lock(self.config, "videos_poll.lock"):
                result = self.apply((client or VideoClient(self.config)).fetch())
        except FetchError as failure:
            if failure.code == "already_running":
                return None, failure
            error = failure
        except Exception:
            error = FetchError("video_internal_error", "视频目录检查失败，请检查网络与本机存储")
        with self.store.connect() as db:
            state = self.store._state(db)
            failures = state.get("video_failures", 0)
            if error:
                failures += 1
                self.store._update(db, video_last_error=str(error), video_error_code=error.code, video_failures=failures)
            delay = next_delay(self.config, failures, error)
            due = time.time() + delay
            self.store._update(db, video_last_attempt=started, video_next_check=due)
        return result, error

    def listing(self, limit=10):
        with self.store.connect() as db:
            rows = [json.loads(r[0]) for r in db.execute("SELECT current_json FROM videos")]
        rows.sort(key=lambda v: (v.get("published_date") or v["created_at"][:10], v["id"]), reverse=True)
        return rows[:limit]

    def status(self):
        with self.store.connect() as db:
            state = self.store._state(db)
            total = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        last = state.get("video_last_success")
        fresh = bool(last) and (datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() <= self.config.interval_seconds * 2 + self.config.request_timeout * 2 + 30
        return {"initialized": state.get("video_initialized", False), "healthy": fresh and not state.get("video_last_error"),
            "stored_videos": total, "current_videos": state.get("video_current_count", 0),
            "last_success": last, "last_error": state.get("video_last_error"), "error_code": state.get("video_error_code"),
            "next_check": state.get("video_next_check"), "interval_seconds": self.config.interval_seconds,
            "latest": self.listing(1)[0] if total else None}
