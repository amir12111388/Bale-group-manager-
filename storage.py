import json
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

try:
    import jdatetime
except Exception:  # pragma: no cover
    jdatetime = None

DEFAULT_GROUP_SETTINGS: Dict[str, Any] = {
    "welcome_enabled": True,
    "welcome_text": "سلام {first_name} عزیز، به گروه خوش آمدی 🌹",
    "rules_text": "قوانین گروه هنوز تنظیم نشده است.",
    "delete_service_messages": False,
    "lock_links": True,
    "lock_bale_links": False,
    "lock_telegram_links": False,
    "lock_instagram_links": False,
    "lock_whatsapp_links": False,
    "lock_site_links": False,
    "lock_channels": False,
    "lock_mentions": False,
    "lock_usernames": False,
    "lock_phone_numbers": False,
    "lock_ads": False,
    "lock_media": False,
    "lock_photos": False,
    "lock_videos": False,
    "lock_voice": False,
    "lock_audio": False,
    "lock_files": False,
    "lock_stickers": False,
    "lock_gifs": False,
    "lock_contacts": False,
    "lock_locations": False,
    "lock_forwards": False,
    "anti_flood_enabled": True,
    "lock_duplicate_messages": False,
    "lock_long_text": False,
    "long_text_limit": 500,
    "lock_short_text": False,
    "short_text_limit": 2,
    "lock_emoji_spam": False,
    "emoji_limit": 10,
    "lock_stretched_chars": False,
    "lock_bad_words": False,
    "lock_bots": False,
    "lock_fake_accounts": False,
    "lock_suspicious_names": False,
    "lock_new_members": False,
    "new_member_watch_seconds": 300,
    "lock_title_change": False,
    "lock_photo_change": False,
    "lock_join_messages": False,
    "lock_leave_messages": False,
    "lock_chat": False,
    "lock_night": False,
    "night_start_hour": 0,
    "night_end_hour": 8,
    "flood_limit": 6,
    "flood_window": 8,
    "warn_limit": 3,
    "force_join_enabled": False,
    "log_chat_id": None,
    "auto_delete_bot_replies": False,
}


def now_ts() -> int:
    return int(time.time())


def jalali_date(ts: Optional[int] = None) -> str:
    ts = ts or now_ts()
    if jdatetime:
        return jdatetime.datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M")
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


class Storage:
    """SQLite + JSON settings storage.

    The bot keeps group-specific settings in a JSON column while critical indexes
    remain normalized. This matches SQLJSON style and stays easy to backup/restore.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            cur = self.conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_admins (
                    user_id INTEGER PRIMARY KEY,
                    added_by INTEGER,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS groups (
                    group_id INTEGER PRIMARY KEY,
                    title TEXT,
                    settings_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS warnings (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    reasons_json TEXT NOT NULL DEFAULT '[]',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(group_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS muted_users (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    until_ts INTEGER,
                    reason TEXT,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(group_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS filters (
                    group_id INTEGER NOT NULL,
                    trigger TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(group_id, trigger)
                );
                CREATE TABLE IF NOT EXISTS notes (
                    group_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(group_id, name)
                );
                CREATE TABLE IF NOT EXISTS required_channels (
                    group_id INTEGER NOT NULL,
                    channel_id TEXT NOT NULL,
                    title TEXT,
                    invite_link TEXT,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(group_id, channel_id)
                );
                CREATE TABLE IF NOT EXISTS activity (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    first_name TEXT,
                    username TEXT,
                    msg_count INTEGER NOT NULL DEFAULT 0,
                    last_seen INTEGER NOT NULL,
                    PRIMARY KEY(group_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER,
                    user_id INTEGER,
                    action TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_logs_group_time ON logs(group_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_activity_group_count ON activity(group_id, msg_count DESC);
                """
            )
            cur.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (str(key),)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (str(key), str(value)),
            )

    def add_owner_admins(self, owner_ids: Iterable[int]) -> None:
        for uid in owner_ids:
            if uid:
                self.add_bot_admin(int(uid), 0)

    def is_bot_admin(self, user_id: int) -> bool:
        row = self.conn.execute("SELECT user_id FROM bot_admins WHERE user_id=?", (int(user_id),)).fetchone()
        return row is not None

    def add_bot_admin(self, user_id: int, added_by: int) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO bot_admins(user_id, added_by, created_at) VALUES(?,?,?)",
                (int(user_id), int(added_by or 0), now_ts()),
            )
            self.log(None, user_id, "bot_admin_add", {"added_by": added_by})

    def remove_bot_admin(self, user_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM bot_admins WHERE user_id=?", (int(user_id),))
            self.log(None, user_id, "bot_admin_remove", {})
            return cur.rowcount > 0

    def list_bot_admins(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM bot_admins ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def ensure_group(self, group_id: int, title: str = "") -> Dict[str, Any]:
        existing = self.conn.execute("SELECT * FROM groups WHERE group_id=?", (int(group_id),)).fetchone()
        ts = now_ts()
        if existing:
            if title and title != existing["title"]:
                self.conn.execute("UPDATE groups SET title=?, updated_at=? WHERE group_id=?", (title, ts, int(group_id)))
            return self.get_group_settings(group_id)
        with self._lock:
            self.conn.execute(
                "INSERT INTO groups(group_id, title, settings_json, created_at, updated_at) VALUES(?,?,?,?,?)",
                (int(group_id), title, json.dumps(DEFAULT_GROUP_SETTINGS, ensure_ascii=False), ts, ts),
            )
        return DEFAULT_GROUP_SETTINGS.copy()

    def get_group_settings(self, group_id: int) -> Dict[str, Any]:
        row = self.conn.execute("SELECT settings_json FROM groups WHERE group_id=?", (int(group_id),)).fetchone()
        if not row:
            return self.ensure_group(group_id)
        try:
            data = json.loads(row["settings_json"] or "{}")
        except Exception:
            data = {}
        merged = DEFAULT_GROUP_SETTINGS.copy()
        merged.update(data)
        return merged

    def update_group_setting(self, group_id: int, key: str, value: Any) -> None:
        with self._lock:
            settings = self.get_group_settings(group_id)
            settings[key] = value
            self.conn.execute(
                "UPDATE groups SET settings_json=?, updated_at=? WHERE group_id=?",
                (json.dumps(settings, ensure_ascii=False), now_ts(), int(group_id)),
            )
            self.log(group_id, None, "setting_update", {"key": key, "value": value})

    def get_group_title(self, group_id: int) -> str:
        row = self.conn.execute("SELECT title FROM groups WHERE group_id=?", (int(group_id),)).fetchone()
        return row["title"] if row and row["title"] else str(group_id)

    def warn_user(self, group_id: int, user_id: int, reason: str, admin_id: Optional[int]) -> int:
        with self._lock:
            row = self.conn.execute("SELECT * FROM warnings WHERE group_id=? AND user_id=?", (int(group_id), int(user_id))).fetchone()
            reasons = []
            count = 0
            if row:
                count = int(row["count"])
                try:
                    reasons = json.loads(row["reasons_json"] or "[]")
                except Exception:
                    reasons = []
            count += 1
            reasons.append({"reason": reason, "admin_id": admin_id, "at": now_ts(), "date": jalali_date()})
            self.conn.execute(
                "INSERT OR REPLACE INTO warnings(group_id,user_id,count,reasons_json,updated_at) VALUES(?,?,?,?,?)",
                (int(group_id), int(user_id), count, json.dumps(reasons[-20:], ensure_ascii=False), now_ts()),
            )
            self.log(group_id, user_id, "warn", {"reason": reason, "admin_id": admin_id, "count": count})
            return count

    def clear_warnings(self, group_id: int, user_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM warnings WHERE group_id=? AND user_id=?", (int(group_id), int(user_id)))
            self.log(group_id, user_id, "warnings_clear", {})

    def get_warnings(self, group_id: int, user_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM warnings WHERE group_id=? AND user_id=?", (int(group_id), int(user_id))).fetchone()
        if not row:
            return {"count": 0, "reasons": []}
        try:
            reasons = json.loads(row["reasons_json"] or "[]")
        except Exception:
            reasons = []
        return {"count": int(row["count"]), "reasons": reasons}

    def mute_user(self, group_id: int, user_id: int, until_ts: Optional[int], reason: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO muted_users(group_id,user_id,until_ts,reason,created_at) VALUES(?,?,?,?,?)",
                (int(group_id), int(user_id), int(until_ts) if until_ts else None, reason, now_ts()),
            )
            self.log(group_id, user_id, "mute", {"until_ts": until_ts, "reason": reason})

    def unmute_user(self, group_id: int, user_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM muted_users WHERE group_id=? AND user_id=?", (int(group_id), int(user_id)))
            self.log(group_id, user_id, "unmute", {})
            return cur.rowcount > 0

    def is_muted(self, group_id: int, user_id: int) -> bool:
        row = self.conn.execute("SELECT until_ts FROM muted_users WHERE group_id=? AND user_id=?", (int(group_id), int(user_id))).fetchone()
        if not row:
            return False
        until_ts = row["until_ts"]
        if until_ts and int(until_ts) <= now_ts():
            self.unmute_user(group_id, user_id)
            return False
        return True

    def add_filter(self, group_id: int, trigger: str, response: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO filters(group_id,trigger,response,created_at) VALUES(?,?,?,?)",
                (int(group_id), trigger.lower().strip(), response.strip(), now_ts()),
            )

    def remove_filter(self, group_id: int, trigger: str) -> bool:
        cur = self.conn.execute("DELETE FROM filters WHERE group_id=? AND trigger=?", (int(group_id), trigger.lower().strip()))
        return cur.rowcount > 0

    def list_filters(self, group_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT trigger,response FROM filters WHERE group_id=? ORDER BY trigger", (int(group_id),)).fetchall()]

    def match_filter(self, group_id: int, text: str) -> Optional[str]:
        if not text:
            return None
        text_l = text.lower()
        for row in self.conn.execute("SELECT trigger,response FROM filters WHERE group_id=?", (int(group_id),)).fetchall():
            if row["trigger"] in text_l:
                return row["response"]
        return None

    def add_note(self, group_id: int, name: str, text: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO notes(group_id,name,text,created_at) VALUES(?,?,?,?)",
            (int(group_id), name.lower().strip(), text.strip(), now_ts()),
        )

    def get_note(self, group_id: int, name: str) -> Optional[str]:
        row = self.conn.execute("SELECT text FROM notes WHERE group_id=? AND name=?", (int(group_id), name.lower().strip())).fetchone()
        return row["text"] if row else None

    def remove_note(self, group_id: int, name: str) -> bool:
        cur = self.conn.execute("DELETE FROM notes WHERE group_id=? AND name=?", (int(group_id), name.lower().strip()))
        return cur.rowcount > 0

    def list_notes(self, group_id: int) -> list[str]:
        return [r["name"] for r in self.conn.execute("SELECT name FROM notes WHERE group_id=? ORDER BY name", (int(group_id),)).fetchall()]

    def add_required_channel(self, group_id: int, channel_id: str, title: str = "", invite_link: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO required_channels(group_id,channel_id,title,invite_link,created_at) VALUES(?,?,?,?,?)",
            (int(group_id), channel_id.strip(), title.strip(), invite_link.strip(), now_ts()),
        )
        self.update_group_setting(group_id, "force_join_enabled", True)

    def remove_required_channel(self, group_id: int, channel_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM required_channels WHERE group_id=? AND channel_id=?", (int(group_id), channel_id.strip()))
        if not self.list_required_channels(group_id):
            self.update_group_setting(group_id, "force_join_enabled", False)
        return cur.rowcount > 0

    def list_required_channels(self, group_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM required_channels WHERE group_id=? ORDER BY created_at", (int(group_id),)).fetchall()
        return [dict(r) for r in rows]

    def record_activity(self, group_id: int, user: dict) -> None:
        if not user:
            return
        uid = int(user.get("id"))
        first = user.get("first_name") or user.get("firstName") or ""
        username = user.get("username") or ""
        self.conn.execute(
            """
            INSERT INTO activity(group_id,user_id,first_name,username,msg_count,last_seen)
            VALUES(?,?,?,?,1,?)
            ON CONFLICT(group_id,user_id) DO UPDATE SET
              first_name=excluded.first_name,
              username=excluded.username,
              msg_count=msg_count+1,
              last_seen=excluded.last_seen
            """,
            (int(group_id), uid, first, username, now_ts()),
        )

    def group_stats(self, group_id: int) -> dict:
        total_msgs = self.conn.execute("SELECT COALESCE(SUM(msg_count),0) AS c FROM activity WHERE group_id=?", (int(group_id),)).fetchone()["c"]
        active_users = self.conn.execute("SELECT COUNT(*) AS c FROM activity WHERE group_id=?", (int(group_id),)).fetchone()["c"]
        warnings = self.conn.execute("SELECT COALESCE(SUM(count),0) AS c FROM warnings WHERE group_id=?", (int(group_id),)).fetchone()["c"]
        muted = self.conn.execute("SELECT COUNT(*) AS c FROM muted_users WHERE group_id=?", (int(group_id),)).fetchone()["c"]
        top = [dict(r) for r in self.conn.execute(
            "SELECT user_id,first_name,username,msg_count FROM activity WHERE group_id=? ORDER BY msg_count DESC LIMIT 10",
            (int(group_id),),
        ).fetchall()]
        return {"total_msgs": int(total_msgs), "active_users": int(active_users), "warnings": int(warnings), "muted": int(muted), "top": top}

    def log(self, group_id: Optional[int], user_id: Optional[int], action: str, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO logs(group_id,user_id,action,data_json,created_at) VALUES(?,?,?,?,?)",
            (group_id, user_id, action, json.dumps(data, ensure_ascii=False), now_ts()),
        )

    def backup_zip(self, backup_dir: str | Path = "backups") -> Path:
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        name = f"bale_group_backup_{time.strftime('%Y%m%d_%H%M%S')}.zip"
        target = backup_dir / name
        # SQLite backup API to avoid corrupt WAL copies.
        tmp_db = backup_dir / f"tmp_{os.getpid()}_{int(time.time())}.sqlite3"
        with sqlite3.connect(str(tmp_db)) as dest:
            self.conn.backup(dest)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, arcname="data/bale_group_manager.sqlite3")
            manifest = {"created_at": now_ts(), "created_jalali": jalali_date(), "type": "bale_group_manager_backup"}
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        tmp_db.unlink(missing_ok=True)
        return target

    def restore_from_zip(self, zip_path: str | Path) -> None:
        zip_path = Path(zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            db_member = "data/bale_group_manager.sqlite3"
            if db_member not in names:
                raise ValueError("backup zip does not contain data/bale_group_manager.sqlite3")
            tmp_dir = self.db_path.parent / f"restore_tmp_{int(time.time())}"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            try:
                zf.extract(db_member, tmp_dir)
                extracted = tmp_dir / db_member
                self.conn.close()
                shutil.copy2(extracted, self.db_path)
                self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
                self.conn.row_factory = sqlite3.Row
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
                self.conn.execute("PRAGMA foreign_keys=ON")
                self.migrate()
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
