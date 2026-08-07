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
    "interest_score", "labels", "starred", "watch_later",
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


# Interest labels (user preference, 2026-07-29): unlike FILTER_CATEGORIES
# these don't hide anything — they're informational tags used to mark and
# later recommend videos. A video can match multiple categories (unlike
# _classify_filter's single-category early-return), so this returns a list.
LABEL_CATEGORIES: dict[str, list[str]] = {
    "ai_tech": ["人工智能", "大模型", "大语言模型", "chatgpt", "claude", "gpt-", "llm",
                "transformer", " ai ", "#ai", "ai泡沫", "ai agent", "科技新闻"],
    "programming": ["前端", "后端", "程序员", "算法", "leetcode", "数据结构", "编程",
                    "javascript", "python", " java", "代码", "开发工程师",
                    "postgres", "devops", "code review", "database", "software engineer"],
    "english_learning": ["英语", "雅思", "口语", "听力", "语法", "外教", "english learning",
                         "ielts", "b1 listening", "vocabulary"],
    "psychology": ["认知", "思维", "人生感悟", "心理", "情绪", "拖延", "自我提升", "本质"],
    "relationships": ["npd", "自恋型人格障碍", "有毒关系", "亲密关系", "回避型",
                      "toxic relationship", "narcissist"],
    "financial": ["股票", "财经", "经济", "债务", "投资", "基金", "通胀", "gdp",
                  "stock market", "economy", "隐性债务", "理财", "收入划分", "月入"],
    "math_science": ["数学", "数学思维", "科普", "物理", "化学", "天文学", "猜想", "math",
                     "science", "physics"],
    "explainer": ["讲解", "解读", "拆解", "原理", "是如何工作的", "how it works", "explained",
                  "深度解析", "全预览"],
    "fitness": ["hiit", "燃脂", "腹肌", "腹部训练", "核心力量", "瘦腰", "暴汗", "站立训练",
                "有氧运动"],
    "news": ["国际局势", "美军", "战争", "空袭", "反击", "military", "breaking news"],
    "relaxation": ["白噪音", "助眠", "雨声", "催眠", "解压", "white noise", "rain sounds",
                   "insomnia"],
}


def _classify_labels(title: str, content: str) -> list[str]:
    text = f"{title} {content}".lower()
    return [
        category
        for category, keywords in LABEL_CATEGORIES.items()
        if any(kw.lower() in text for kw in keywords)
    ]


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
            fetched_at TEXT,
            interest_score INTEGER,
            labels TEXT,
            starred INTEGER NOT NULL DEFAULT 0,
            watch_later INTEGER NOT NULL DEFAULT 0
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
            updated_at TEXT,
            unlisted INTEGER NOT NULL DEFAULT 0
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
    if "interest_score" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN interest_score INTEGER")
    if "labels" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN labels TEXT")
    if "starred" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN starred INTEGER NOT NULL DEFAULT 0")
    if "watch_later" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN watch_later INTEGER NOT NULL DEFAULT 0")

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
        creator_cols = {row[1] for row in conn.execute("PRAGMA table_info(creators)")}
    if "unlisted" not in creator_cols:
        conn.execute("ALTER TABLE creators ADD COLUMN unlisted INTEGER NOT NULL DEFAULT 0")
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
        labels = ",".join(_classify_labels(v.get("title", ""), v.get("content", "")))
        conn.execute(
            "INSERT INTO videos "
            "(id, platform, url, title, user, published_at, content, thumbnail_url, play_url, filtered_category, watched, fetched_at, labels) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "play_url = COALESCE(NULLIF(excluded.play_url, ''), play_url), "
            "thumbnail_url = COALESCE(NULLIF(excluded.thumbnail_url, ''), thumbnail_url)",
            (
                v["id"], platform, v["url"], v["title"], v["user"], v["published_at"],
                v["content"], v.get("thumbnail_url", ""), v.get("play_url", ""), category, now, labels,
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
