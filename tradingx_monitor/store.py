from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def changed(previous, current):
    """Counts, thumbnails and access changes alone are not content edits."""
    if previous["editedAt"] != current["editedAt"]:
        return True
    if not previous["locked"] and not current["locked"] and previous["text"] != current["text"]:
        return True
    return (
        previous["textLength"] != current["textLength"]
        or [i.get("id") for i in previous["images"]] != [i.get("id") for i in current["images"]]
    )


class Store:
    def __init__(self, config):
        self.config = config
        config.prepare()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    current_json TEXT NOT NULL, full_json TEXT, full_seen TEXT
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL,
                    observed_at TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL,
                    event_type TEXT NOT NULL, post_id INTEGER, details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS polls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL, ok INTEGER NOT NULL, post_count INTEGER,
                    new_count INTEGER, changed_count INTEGER, error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS revisions_post ON revisions(post_id, id);
            """)
        config.database.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.config.database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _state(db):
        return {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT key, value FROM state")}

    @staticmethod
    def _update(db, **values):
        db.executemany("INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       [(k, encode(v)) for k, v in values.items()])

    @staticmethod
    def _event(db, when, kind, post_id=None, **details):
        db.execute("INSERT INTO events(observed_at,event_type,post_id,details_json) VALUES(?,?,?,?)",
                   (when, kind, post_id, encode(details)))

    @staticmethod
    def _trim_polls(db):
        db.execute("DELETE FROM polls WHERE id <= (SELECT COALESCE(MAX(id),0)-1000 FROM polls)")

    def success(self, snapshot, started_at):
        when, new_count, changed_count = now(), 0, 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old_state = self._state(db)
            baseline = not old_state.get("initialized", False)
            for post in sorted(snapshot["posts"], key=lambda p: p["id"]):
                row = db.execute("SELECT * FROM posts WHERE id=?", (post["id"],)).fetchone()
                payload = encode(post)
                full = None if post["locked"] else payload
                kind = None
                if row is None:
                    db.execute("INSERT INTO posts VALUES(?,?,?,?,?,?)",
                               (post["id"], when, when, payload, full, when if full else None))
                    kind = "baseline" if baseline else "new_post"
                    if not baseline:
                        new_count += 1
                else:
                    previous = json.loads(row["current_json"])
                    if previous["locked"] and not post["locked"]:
                        kind = "unlocked"
                    elif changed(previous, post):
                        kind = "updated"
                    db.execute("UPDATE posts SET last_seen=?,current_json=?,full_json=COALESCE(?,full_json),"
                               "full_seen=CASE WHEN ? IS NOT NULL THEN ? ELSE full_seen END WHERE id=?",
                               (when, payload, full, full, when, post["id"]))
                    if kind:
                        changed_count += 1
                if kind:
                    db.execute("INSERT INTO revisions(post_id,observed_at,kind,payload_json) VALUES(?,?,?,?)",
                               (post["id"], when, kind, payload))
                    if kind != "baseline":
                        self._event(db, when, kind, post["id"], url=post["url"],
                                    locked=post["locked"], createdAt=post["createdAt"], editedAt=post["editedAt"])
            if baseline:
                self._event(db, when, "baseline", count=len(snapshot["posts"]))
            elif old_state.get("viewer") != snapshot["viewer"]:
                self._event(db, when, "access_changed", previous=old_state.get("viewer"), viewer=snapshot["viewer"])
            if old_state.get("consecutive_failures", 0):
                self._event(db, when, "recovered")
            locked_count = sum(p["locked"] for p in snapshot["posts"])
            warning = None
            if snapshot["auth_configured"] and (snapshot["viewer"] not in ("member", "owner") or locked_count):
                warning = "已配置凭据，但部分或全部正文仍受限；请检查会员有效期和登录态"
            if snapshot["truncated"]:
                warning = (warning + "；" if warning else "") + "已达到分页上限，较旧帖子尚未覆盖"
            self._update(db, initialized=True, last_attempt=started_at, last_success=when,
                         last_error=None, error_code=None, consecutive_failures=0,
                         viewer=snapshot["viewer"], auth_configured=snapshot["auth_configured"],
                         source_total=snapshot["total"], pages=snapshot["pages"], truncated=snapshot["truncated"],
                         last_post_count=len(snapshot["posts"]), last_locked_count=locked_count,
                         warning=warning, next_poll_at=None)
            db.execute("INSERT INTO polls(started_at,finished_at,ok,post_count,new_count,changed_count) VALUES(?,?,?,?,?,?)",
                       (started_at, when, 1, len(snapshot["posts"]), new_count, changed_count))
            self._trim_polls(db)
        return {"baseline": baseline, "posts": len(snapshot["posts"]), "new": new_count, "changed": changed_count,
                "viewer": snapshot["viewer"], "warning": warning}

    def failure(self, error, started_at):
        when = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db)
            failures = state.get("consecutive_failures", 0) + 1
            if state.get("error_code") != error.code:
                self._event(db, when, "monitor_error", code=error.code, message=str(error))
            self._update(db, last_attempt=started_at, last_error=str(error), error_code=error.code,
                         consecutive_failures=failures, next_poll_at=None)
            db.execute("INSERT INTO polls(started_at,finished_at,ok,error_code) VALUES(?,?,?,?)",
                       (started_at, when, 0, error.code))
            self._trim_polls(db)
        return failures

    def schedule(self, next_poll_at):
        with self.connect() as db:
            self._update(db, next_poll_at=next_poll_at)

    def status(self):
        with self.connect() as db:
            state = self._state(db)
            state["stored_posts"] = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            state["readable_archived_posts"] = db.execute("SELECT COUNT(*) FROM posts WHERE full_json IS NOT NULL").fetchone()[0]
            state["event_count"] = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            state["latest_post"] = None
            row = db.execute("SELECT current_json FROM posts ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                post = json.loads(row[0])
                state["latest_post"] = {k: post[k] for k in ("id", "createdAt", "editedAt", "locked", "url")}
        state["interval_seconds"] = self.config.interval_seconds
        state["dashboard_url"] = f"http://127.0.0.1:{self.config.port}"
        state["healthy"] = False
        if state.get("last_success") and not state.get("last_error"):
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["last_success"].replace("Z", "+00:00"))).total_seconds()
            state["healthy"] = age <= self.config.interval_seconds * 2 + self.config.request_timeout * self.config.max_pages + 30
        return state

    def posts(self, limit=50):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            post = json.loads(row["current_json"])
            post.update(firstSeen=row["first_seen"], lastSeen=row["last_seen"], cachedText=None, cachedAt=None)
            if post["locked"] and row["full_json"]:
                post["cachedText"] = json.loads(row["full_json"])["text"]
                post["cachedAt"] = row["full_seen"]
            result.append(post)
        return result

    def events(self, limit=50, after=None):
        with self.connect() as db:
            # With a cursor, return oldest unseen first so a consumer cannot skip a burst.
            order = "ASC" if after is not None else "DESC"
            rows = db.execute(f"SELECT * FROM events WHERE id > ? ORDER BY id {order} LIMIT ?", (after or 0, limit)).fetchall()
        return [{"id": r["id"], "observedAt": r["observed_at"], "type": r["event_type"],
                 "postId": r["post_id"], "details": json.loads(r["details_json"])} for r in rows]

    def revisions(self, post_id, limit=100):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM revisions WHERE post_id=? ORDER BY id DESC LIMIT ?", (post_id, limit)).fetchall()
        return [{"id": r["id"], "observedAt": r["observed_at"], "type": r["kind"],
                 "post": json.loads(r["payload_json"])} for r in rows]
