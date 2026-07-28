"""Shared config, SQLite store, and upsert helpers used by both server.py
(Douyin) and youtube.py. Split out to avoid a circular import between the
two platform modules — server.py wires MCP tools/routes for both, so it
must import youtube.py, which in turn needs the DB helpers here.
"""

import datetime
import os
import sqlite3
from typing import Optional

DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "videos.db")

DOUYIN_STORAGE_STATE_PATH = os.path.join(DATA_DIR, "storage_state.json")
YOUTUBE_STORAGE_STATE_PATH = os.path.join(DATA_DIR, "youtube_storage_state.json")
QR_IMAGE_PATH = os.path.join(DATA_DIR, "qrcode.png")

os.makedirs(DATA_DIR, exist_ok=True)

_VIDEO_COLUMNS = [
    "id", "platform", "url", "title", "user", "published_at", "content",
    "thumbnail_url", "play_url", "filtered_category", "watched", "fetched_at",
]

# Content filters (user preference, 2026-07-28): hide videos primarily about
# these topics from the default UI view. Keyword match on title+content,
# case-insensitive, applies across all platforms. Edit to add/remove categories.
FILTER_CATEGORIES: dict[str, list[str]] = {
    "drawing": ["绘画", "画画", "美术", "手绘", "素描", "水彩", "马克笔", "临摹", "画笔",
                "drawing tutorial", "painting tutorial", "sketch tutorial"],
    "gym": ["健身", "胸肌", "背肌", "腹肌", "深蹲", "卧推", "撸铁", "增肌", "训练动作",
            "workout", "gym "],
}


def _classify_filter(title: str, content: str) -> Optional[str]:
    text = f"{title} {content}".lower()
    for category, keywords in FILTER_CATEGORIES.items():
        for kw in keywords:
            if kw.lower() in text:
                return category
    return None


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL DEFAULT 'douyin',
            url TEXT,
            title TEXT,
            user TEXT,
            published_at TEXT,
            content TEXT,
            thumbnail_url TEXT,
            play_url TEXT,
            filtered_category TEXT,
            watched INTEGER NOT NULL DEFAULT 0,
            fetched_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS creators (
            name TEXT PRIMARY KEY,
            platform TEXT NOT NULL DEFAULT 'douyin',
            avatar_url TEXT,
            unread_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
        """
    )
    # CREATE TABLE IF NOT EXISTS doesn't add columns/change the PK of a table
    # created by an earlier schema version — migrate in place instead of
    # dropping data.
    video_cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "play_url" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN play_url TEXT")
    if "platform" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN platform TEXT NOT NULL DEFAULT 'douyin'")

    creator_cols = {row[1] for row in conn.execute("PRAGMA table_info(creators)")}
    if "platform" not in creator_cols:
        # `name` alone was the PK pre-YouTube; a creator name could in
        # principle collide across platforms, so widen the key to
        # (platform, name). SQLite can't ALTER a primary key in place —
        # rebuild the table.
        conn.execute(
            """
            CREATE TABLE creators_new (
                name TEXT,
                platform TEXT NOT NULL DEFAULT 'douyin',
                avatar_url TEXT,
                unread_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (platform, name)
            )
            """
        )
        conn.execute(
            "INSERT INTO creators_new (name, platform, avatar_url, unread_count, updated_at) "
            "SELECT name, 'douyin', avatar_url, unread_count, updated_at FROM creators"
        )
        conn.execute("DROP TABLE creators")
        conn.execute("ALTER TABLE creators_new RENAME TO creators")
    conn.commit()
    return conn


def upsert_videos(videos: list[dict], platform: str) -> tuple[int, int]:
    """Insert new videos; for ones already in the DB, backfill play_url/
    thumbnail_url if they were empty (covers rows synced before those fields
    existed) without touching watched state. Returns (new_count, total)."""
    conn = _db()
    if not videos:
        total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        conn.close()
        return 0, total

    ids = [v["id"] for v in videos]
    placeholders = ",".join("?" * len(ids))
    existing_ids = {
        row[0] for row in conn.execute(f"SELECT id FROM videos WHERE id IN ({placeholders})", ids)
    }

    now = datetime.datetime.now().isoformat(timespec="seconds")
    for v in videos:
        category = _classify_filter(v.get("title", ""), v.get("content", ""))
        conn.execute(
            "INSERT INTO videos "
            "(id, platform, url, title, user, published_at, content, thumbnail_url, play_url, filtered_category, watched, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "play_url = COALESCE(NULLIF(excluded.play_url, ''), play_url), "
            "thumbnail_url = COALESCE(NULLIF(excluded.thumbnail_url, ''), thumbnail_url)",
            (
                v["id"], platform, v["url"], v["title"], v["user"], v["published_at"],
                v["content"], v.get("thumbnail_url", ""), v.get("play_url", ""), category, now,
            ),
        )
    conn.commit()
    new_count = len(ids) - len(existing_ids)
    total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    conn.close()
    return new_count, total


def upsert_creators(creators: list[dict], platform: str) -> None:
    if not creators:
        return
    conn = _db()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for c in creators:
        conn.execute(
            "INSERT INTO creators (name, platform, avatar_url, unread_count, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(platform, name) DO UPDATE SET avatar_url=excluded.avatar_url, "
            "unread_count=excluded.unread_count, updated_at=excluded.updated_at",
            (c["name"], platform, c.get("avatar_url", ""), c.get("unread_count", 0), now),
        )
    conn.commit()
    conn.close()
