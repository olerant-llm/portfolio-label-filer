import sqlite3
from datetime import datetime, timezone
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent


def get_db_path(root: Path) -> Path:
    return root / "data" / "gallery.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id INTEGER NOT NULL REFERENCES folders(id),
    full_path TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    thumbnail_path TEXT,
    media_type TEXT DEFAULT 'image',
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS image_tags (
    image_id INTEGER NOT NULL REFERENCES images(id),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    source TEXT NOT NULL,
    score REAL,
    PRIMARY KEY (image_id, tag_id, source)
);

CREATE TABLE IF NOT EXISTS presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tag_names TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def get_connection(root: Path) -> sqlite3.Connection:
    db_path = get_db_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    try:
        conn.execute("ALTER TABLE tags ADD COLUMN ja TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 既にja列がある場合
    try:
        conn.execute("ALTER TABLE tags ADD COLUMN is_character INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 既にis_character列がある場合（未使用。danbooru_20250919.csvベースのcategory列に統合済み）
    try:
        conn.execute("ALTER TABLE tags ADD COLUMN category INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 既にcategory列がある場合
    try:
        conn.execute("ALTER TABLE images ADD COLUMN media_type TEXT DEFAULT 'image'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 既にmedia_type列がある場合
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
