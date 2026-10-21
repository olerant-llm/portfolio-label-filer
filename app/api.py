import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

import webview
from PIL import Image, UnidentifiedImageError

import db
import library
from tag_category import get_category, get_post_count
from tag_dictionary import load_seed_translations, resolve_translation

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
TEXT_EXTS = {".txt", ".md", ".log", ".pdf"}
SUPPORTED_EXTS = IMAGE_EXTS | TEXT_EXTS
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CACHE_DIR = STATIC_DIR / "cache"
THUMB_SIZE = (320, 320)
MAX_TAGGING_WORKERS = 6  # メモリを使いすぎないよう、コア数が多いパソコンでも上限を設ける


def _make_text_thumbnail(path: Path, thumb_path: Path) -> None:
    """テキスト/PDFの内容とファイル名を描画したサムネイル画像をキャッシュに生成する"""
    import tagger_text_nlp
    from PIL import ImageDraw, ImageFont

    # テキスト抽出
    text = tagger_text_nlp.extract_text(path)
    
    # プレビュー文を作成
    preview = text.strip()[:200]
    
    # キャンバス作成 (320x320, 白背景)
    im = Image.new("RGB", (320, 320), "white")
    draw = ImageDraw.Draw(im)
    
    # フォント設定（Windows標準のMSゴシックを使用）
    font_path = r"C:\Windows\Fonts\msgothic.ttc"
    try:
        font_title = ImageFont.truetype(font_path, 16)
        font_body = ImageFont.truetype(font_path, 12)
    except Exception:
        font_title = ImageFont.load_default()
        font_body = ImageFont.load_default()
        
    # ファイル名の描画 (上部)
    title = path.name
    # 長いファイル名は切り詰める
    if len(title) > 22:
        title = title[:19] + "..."
    draw.text((15, 15), title, fill="black", font=font_title)
    draw.line((15, 38, 305, 38), fill="gray")
    
    # 本文の描画 (改行と折り返し)
    y = 50
    line_len = 22  # 1行あたりの文字数目安
    lines = []
    for raw_line in preview.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        # 1行が長い場合は折り返す
        for i in range(0, len(raw_line), line_len):
            lines.append(raw_line[i : i + line_len])
            if len(lines) >= 13:  # サムネイル内に収まる行数で打ち切り
                break
        if len(lines) >= 13:
            break
            
    for line in lines:
        draw.text((15, y), line, fill="dimgray", font=font_body)
        y += 18
        
    # 外枠線を描く (TagSpaces風のデザインアクセント)
    draw.rectangle((0, 0, 319, 319), outline="lightgray", width=2)
    
    im.save(thumb_path, "JPEG", quality=85)



def _tagging_worker_count() -> int:
    cpu = os.cpu_count() or 1
    return max(1, min(cpu - 1, MAX_TAGGING_WORKERS))


class Api:
    def __init__(self):
        self.conn = None
        self.root: Path | None = None
        self.window = None  # set by main.py after window creation
        self._db_lock = threading.Lock()
        self._tagging_state = {
            "running": False,
            "total": 0,
            "done": 0,
            "current": "",
            "error": None,
            "workers": 0,
            "cancel_requested": False,
            "cancelled": False,
        }
        self._migrate_legacy_db_if_needed()
        settings = library.load_settings()
        active = settings.get("active_root")
        if active and Path(active).is_dir():
            self._open_library_internal(Path(active))

    # ---- library (root folder = 1 database) ----

    def get_app_state(self):
        settings = library.load_settings()
        return {
            "active_root": str(self.root) if self.root else None,
            "recent_roots": settings.get("recent_roots", []),
        }

    def pick_folder(self):
        if self.window is None:
            return None
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None

    def open_library(self, path: str):
        if self._tagging_state["running"]:
            return {
                "ok": False,
                "error": "タグ付けを停止してからライブラリを切り替えてください",
                "code": "tagging_running",
            }
        if not path or not Path(path).is_dir():
            return {"ok": False, "error": "フォルダが見つかりません", "code": "folder_not_found"}
        self._open_library_internal(Path(path))
        library.remember_root(str(self.root))
        return {"ok": True, "root": str(self.root)}

    def remove_library_history(self, path: str):
        if not path:
            return {"ok": False, "error": "履歴の場所が指定されていません", "code": "path_required"}
        settings = library.forget_root(path)
        return {"ok": True, "recent_roots": settings["recent_roots"]}

    def _open_library_internal(self, root: Path) -> None:
        self.root = root.resolve()
        self.conn = db.get_connection(self.root)
        self._ensure_root_folder()
        self._backfill_tag_categories()

    def _ensure_root_folder(self) -> int:
        row = self.conn.execute("SELECT id FROM folders WHERE path = '.'").fetchone()
        if row:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO folders (path, added_at) VALUES ('.', ?)", (db.now_iso(),)
        )
        self.conn.commit()
        return cur.lastrowid

    def _no_library(self):
        return {"ok": False, "error": "ライブラリが開かれていません", "code": "no_library"}

    def _migrate_legacy_db_if_needed(self) -> None:
        legacy_db = db.TOOL_DIR / "data" / "gallery.db"
        if library.SETTINGS_PATH.exists() or not legacy_db.exists():
            return
        try:
            self._do_migrate_legacy_db(legacy_db)
        except Exception:
            traceback.print_exc()
            library.save_settings({"active_root": None, "recent_roots": []})

    def _do_migrate_legacy_db(self, legacy_db: Path) -> None:
        old_conn = sqlite3.connect(legacy_db)
        old_conn.row_factory = sqlite3.Row
        old_import_dir = db.TOOL_DIR / "imported_images"
        import_dir_resolved = str(old_import_dir.resolve())
        rows = old_conn.execute("SELECT id, path FROM folders").fetchall()
        old_conn.close()

        real_folders = [r for r in rows if r["path"] != import_dir_resolved]
        if len(real_folders) != 1:
            library.save_settings({"active_root": None, "recent_roots": []})
            return
        root = Path(real_folders[0]["path"])
        if not root.is_dir():
            library.save_settings({"active_root": None, "recent_roots": []})
            return
        root = root.resolve()

        # ドロップ取り込み済みの画像があれば、新しい起点フォルダの下に移す
        moved_import: dict[str, Path] = {}
        if old_import_dir.is_dir():
            new_import_dir = root / "imported_images"
            new_import_dir.mkdir(parents=True, exist_ok=True)
            for item in old_import_dir.iterdir():
                if item.is_file():
                    dest = new_import_dir / item.name
                    shutil.move(str(item), str(dest))
                    moved_import[str(item.resolve())] = dest

        new_db_path = db.get_db_path(root)
        new_db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_db), str(new_db_path))
        for suffix in ("-wal", "-shm"):
            side = Path(str(legacy_db) + suffix)
            if side.exists():
                shutil.move(str(side), str(new_db_path) + suffix)

        new_conn = sqlite3.connect(new_db_path)
        new_conn.row_factory = sqlite3.Row
        image_rows = new_conn.execute("SELECT id, full_path FROM images").fetchall()
        for r in image_rows:
            old_full = Path(r["full_path"])
            new_full = moved_import.get(str(old_full), old_full)
            try:
                rel = new_full.resolve().relative_to(root).as_posix()
            except ValueError:
                continue  # 起点フォルダの外にある画像は引き継げない
            new_conn.execute("UPDATE images SET full_path = ? WHERE id = ?", (rel, r["id"]))
        cur = new_conn.execute("DELETE FROM folders")
        cur = new_conn.execute(
            "INSERT INTO folders (path, added_at) VALUES ('.', ?)", (db.now_iso(),)
        )
        new_conn.execute("UPDATE images SET folder_id = ?", (cur.lastrowid,))
        new_conn.commit()
        new_conn.close()

        library.remember_root(str(root))

    def _backfill_tag_categories(self):
        rows = self.conn.execute("SELECT id, name FROM tags WHERE category IS NULL").fetchall()
        if not rows:
            return
        for r in rows:
            category = get_category(r["name"])
            if category is not None:
                self.conn.execute("UPDATE tags SET category = ? WHERE id = ?", (category, r["id"]))
        self.conn.commit()

    # ---- scan ----

    def scan_all(self):
        if self.conn is None:
            return self._no_library()
        folder_id = self._ensure_root_folder()
        added = 0
        skipped = 0
        for root_dir, dirs, files in os.walk(self.root):
            if Path(root_dir) == self.root:
                dirs[:] = [d for d in dirs if d != "data"]
            for name in files:
                if Path(name).suffix.lower() not in SUPPORTED_EXTS:
                    continue
                full_path = Path(root_dir, name).resolve()
                rel = full_path.relative_to(self.root).as_posix()
                existing = self.conn.execute(
                    "SELECT id FROM images WHERE full_path = ?", (rel,)
                ).fetchone()
                if existing:
                    skipped += 1
                    continue
                if self._register_image(folder_id, str(full_path)):
                    added += 1
        removed = self._remove_missing_images()
        return {"ok": True, "added": added, "skipped": skipped, "removed": removed}

    def _remove_missing_images(self) -> int:
        rows = self.conn.execute("SELECT id, full_path, thumbnail_path FROM images").fetchall()
        removed = 0
        for r in rows:
            if (self.root / r["full_path"]).exists():
                continue
            self.conn.execute("DELETE FROM image_tags WHERE image_id = ?", (r["id"],))
            self.conn.execute("DELETE FROM images WHERE id = ?", (r["id"],))
            if r["thumbnail_path"]:
                Path(r["thumbnail_path"]).unlink(missing_ok=True)
            removed += 1
        if removed:
            self.conn.commit()
        return removed

    def _register_image(self, folder_id: int, full_path: str) -> bool:
        path = Path(full_path)
        try:
            stat = path.stat()
            thumb_path = self._make_thumbnail(path)
        except Exception as e:
            # Pillowの画像読み込みエラーはテキストファイルなら無視
            if path.suffix.lower() in TEXT_EXTS:
                try:
                    stat = path.stat()
                    # プレースホルダーまたは文字化け防止でダミー作成
                    key = hashlib.sha1(str(path).encode("utf-8")).hexdigest()
                    thumb_path = CACHE_DIR / f"{key}.jpg"
                    _make_text_thumbnail(path, thumb_path)
                except Exception:
                    return False
            else:
                return False
        rel = path.resolve().relative_to(self.root).as_posix()
        media_type = "text" if path.suffix.lower() in TEXT_EXTS else "image"
        self.conn.execute(
            """INSERT INTO images (folder_id, full_path, filename, size, mtime, thumbnail_path, media_type, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (folder_id, rel, path.name, stat.st_size, stat.st_mtime, str(thumb_path), media_type, db.now_iso()),
        )
        self.conn.commit()
        return True

    def _make_thumbnail(self, path: Path) -> Path:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(str(path).encode("utf-8")).hexdigest()
        thumb_path = CACHE_DIR / f"{key}.jpg"
        if not thumb_path.exists():
            if path.suffix.lower() in TEXT_EXTS:
                _make_text_thumbnail(path, thumb_path)
            else:
                with Image.open(path) as im:
                    im = im.convert("RGB")
                    im.thumbnail(THUMB_SIZE)
                    im.save(thumb_path, "JPEG", quality=85)
        return thumb_path

    # ---- drag & drop import ----

    def import_dropped_file(self, filename: str, data_b64: str):
        if self.conn is None:
            return self._no_library()
        name = Path(filename or "").name
        if not name or Path(name).suffix.lower() not in SUPPORTED_EXTS:
            return {"ok": False, "error": "対応していない形式です", "code": "unsupported_format"}

        import_dir = self.root / "imported_images"
        import_dir.mkdir(parents=True, exist_ok=True)
        dest = import_dir / name
        stem, suffix = dest.stem, dest.suffix
        counter = 1
        while dest.exists():
            dest = import_dir / f"{stem} ({counter}){suffix}"
            counter += 1

        try:
            data = base64.b64decode(data_b64)
            dest.write_bytes(data)
            if dest.suffix.lower() not in TEXT_EXTS:
                with Image.open(dest) as im:
                    im.verify()
        except Exception as e:
            dest.unlink(missing_ok=True)
            return {"ok": False, "error": f"ファイルを読み込めませんでした: {e}", "code": "file_read_failed"}

        folder_id = self._ensure_root_folder()
        if not self._register_image(folder_id, str(dest)):
            dest.unlink(missing_ok=True)
            return {"ok": False, "error": "登録に失敗しました", "code": "register_failed"}
        return {"ok": True}

    # ---- images ----

    def _rows_to_image_dicts(self, rows):
        result = []
        for r in rows:
            thumb_url = None
            if r["thumbnail_path"]:
                rel = Path(r["thumbnail_path"]).relative_to(STATIC_DIR).as_posix()
                thumb_url = rel
            result.append(
                {
                    "id": r["id"],
                    "filename": r["filename"],
                    "full_path": str(self.root / r["full_path"]),
                    "thumbnail_url": thumb_url,
                }
            )
        return result

    def list_images(self):
        if self.conn is None:
            return []
        rows = self.conn.execute(
            "SELECT id, filename, full_path, thumbnail_path FROM images ORDER BY added_at DESC"
        ).fetchall()
        return self._rows_to_image_dicts(rows)

    def rename_image(self, image_id: int, new_filename: str):
        new_filename = (new_filename or "").strip()
        if not new_filename:
            return {"ok": False, "error": "ファイル名を入力してください", "code": "filename_required"}
        if any(c in new_filename for c in '\\/:*?"<>|'):
            return {"ok": False, "error": "ファイル名に使えない文字が含まれています", "code": "filename_invalid_chars"}

        row = self.conn.execute(
            "SELECT full_path FROM images WHERE id = ?", (image_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "画像が見つかりません", "code": "image_not_found"}

        old_path = self.root / row["full_path"]
        new_path = old_path.parent / new_filename
        if new_path == old_path:
            return {"ok": True, "filename": new_filename, "full_path": str(new_path)}
        if new_path.exists():
            return {"ok": False, "error": "同じ名前のファイルが既に存在します", "code": "filename_conflict"}

        try:
            old_path.rename(new_path)
        except OSError as e:
            return {"ok": False, "error": f"ファイル名の変更に失敗しました: {e}", "code": "rename_failed"}

        new_rel = new_path.resolve().relative_to(self.root).as_posix()
        self.conn.execute(
            "UPDATE images SET full_path = ?, filename = ? WHERE id = ?",
            (new_rel, new_path.name, image_id),
        )
        self.conn.commit()
        return {"ok": True, "filename": new_path.name, "full_path": str(new_path)}

    def move_images(self, image_ids: list[int], dest_folder: str):
        if self.conn is None:
            return self._no_library()
        try:
            dest = Path(dest_folder).resolve()
        except OSError:
            return {"ok": False, "error": "移動先フォルダが不正です", "code": "invalid_destination"}
        if not dest.is_dir():
            return {"ok": False, "error": "移動先フォルダが見つかりません", "code": "folder_not_found"}
        try:
            dest.relative_to(self.root)
        except ValueError:
            return {"ok": False, "error": "ライブラリ内のフォルダを選んでください", "code": "destination_outside_library"}

        moved = 0
        failed = []
        for image_id in image_ids:
            row = self.conn.execute("SELECT full_path FROM images WHERE id = ?", (image_id,)).fetchone()
            if not row:
                continue
            old_path = self.root / row["full_path"]
            new_path = dest / old_path.name
            if new_path == old_path:
                moved += 1
                continue
            if new_path.exists():
                failed.append(old_path.name)
                continue
            try:
                old_path.rename(new_path)
            except OSError:
                failed.append(old_path.name)
                continue
            new_rel = new_path.resolve().relative_to(self.root).as_posix()
            self.conn.execute(
                "UPDATE images SET full_path = ?, filename = ? WHERE id = ?",
                (new_rel, new_path.name, image_id),
            )
            moved += 1
        self.conn.commit()
        return {"ok": True, "moved": moved, "failed": failed}

    def delete_images(self, image_ids: list[int]):
        if self.conn is None:
            return self._no_library()
        deleted = 0
        failed = []
        for image_id in image_ids:
            row = self.conn.execute(
                "SELECT full_path, thumbnail_path FROM images WHERE id = ?", (image_id,)
            ).fetchone()
            if not row:
                continue
            path = self.root / row["full_path"]
            try:
                path.unlink(missing_ok=True)
            except OSError:
                failed.append(path.name)
                continue
            if row["thumbnail_path"]:
                Path(row["thumbnail_path"]).unlink(missing_ok=True)
            self.conn.execute("DELETE FROM image_tags WHERE image_id = ?", (image_id,))
            self.conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
            deleted += 1
        self.conn.commit()
        return {"ok": True, "deleted": deleted, "failed": failed}

    def filter_images(self, tag_names: list[str], mode: str = "and"):
        if self.conn is None:
            return []
        if not tag_names:
            return self.list_images()
        placeholders = ",".join("?" for _ in tag_names)
        if mode == "or":
            query = f"""
                SELECT DISTINCT i.id, i.filename, i.full_path, i.thumbnail_path, i.added_at
                FROM images i
                JOIN image_tags it ON it.image_id = i.id
                JOIN tags t ON t.id = it.tag_id
                WHERE t.name IN ({placeholders})
                ORDER BY i.added_at DESC
            """
            rows = self.conn.execute(query, tag_names).fetchall()
        else:
            query = f"""
                SELECT i.id, i.filename, i.full_path, i.thumbnail_path, i.added_at
                FROM images i
                WHERE (
                    SELECT COUNT(DISTINCT t.name)
                    FROM image_tags it JOIN tags t ON t.id = it.tag_id
                    WHERE it.image_id = i.id AND t.name IN ({placeholders})
                ) = ?
                ORDER BY i.added_at DESC
            """
            rows = self.conn.execute(query, [*tag_names, len(tag_names)]).fetchall()
        return self._rows_to_image_dicts(rows)

    def similar_images(self, image_id: int, limit: int = 30):
        rows = self.conn.execute("SELECT DISTINCT image_id, tag_id FROM image_tags").fetchall()
        tagsets: dict[int, set] = {}
        for r in rows:
            tagsets.setdefault(r["image_id"], set()).add(r["tag_id"])

        target = tagsets.get(image_id)
        if not target:
            return []

        scored = []
        for other_id, tagset in tagsets.items():
            if other_id == image_id:
                continue
            inter = len(target & tagset)
            if inter == 0:
                continue
            union_len = len(target | tagset)
            scored.append((other_id, inter / union_len))
        scored.sort(key=lambda x: -x[1])
        top = scored[:limit]
        if not top:
            return []

        ids = [t[0] for t in top]
        score_by_id = dict(top)
        placeholders = ",".join("?" for _ in ids)
        img_rows = self.conn.execute(
            f"SELECT id, filename, full_path, thumbnail_path FROM images WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        by_id = {r["id"]: r for r in img_rows}
        ordered_rows = [by_id[i] for i in ids if i in by_id]
        result = self._rows_to_image_dicts(ordered_rows)
        for d in result:
            d["similarity"] = round(score_by_id[d["id"]], 3)
        return result

    # ---- tags ----

    def list_tags(self):
        if self.conn is None:
            return []
        rows = self.conn.execute(
            """SELECT t.name AS name, t.ja AS ja, t.category AS category,
                      COUNT(DISTINCT it.image_id) AS count
               FROM tags t JOIN image_tags it ON it.tag_id = t.id
               GROUP BY t.id
               ORDER BY count DESC, t.name ASC"""
        ).fetchall()
        seed = load_seed_translations()
        result = []
        for r in rows:
            ja = r["ja"] or resolve_translation(r["name"], seed)
            result.append({
                "name": r["name"],
                "count": r["count"],
                "ja": ja,
                "category": r["category"],
            })
        return result

    def set_tag_translation(self, name: str, ja: str):
        ja = (ja or "").strip()
        row = self.conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if not row:
            return {"ok": False, "error": "タグが見つかりません", "code": "tag_not_found"}
        self.conn.execute("UPDATE tags SET ja = ? WHERE id = ?", (ja or None, row["id"]))
        self.conn.commit()
        return {"ok": True, "ja": ja or None}

    def list_dictionary_tags(self):
        seed = load_seed_translations()
        items = [{"name": name, "ja": ja, "post_count": get_post_count(name)} for name, ja in seed.items()]
        items.sort(key=lambda d: (-d["post_count"], d["name"]))
        return items

    def get_image_tags(self, image_id: int):
        rows = self.conn.execute(
            """SELECT t.name AS name, t.ja AS ja
               FROM image_tags it JOIN tags t ON t.id = it.tag_id
               WHERE it.image_id = ?
               GROUP BY t.id
               ORDER BY t.name ASC""",
            (image_id,),
        ).fetchall()
        seed = load_seed_translations()
        return [{"name": r["name"], "ja": r["ja"] or resolve_translation(r["name"], seed)} for r in rows]

    def add_image_tag(self, image_id: int, tag_name: str):
        tag_name = (tag_name or "").strip()
        if not tag_name:
            return {"ok": False, "error": "タグを入力してください", "code": "tag_empty"}
        row = self.conn.execute("SELECT id FROM images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "画像が見つかりません", "code": "image_not_found"}
        self._save_tags(image_id, [(tag_name, 1.0)], "manual")
        seed = load_seed_translations()
        return {"ok": True, "name": tag_name, "ja": resolve_translation(tag_name, seed)}

    def remove_image_tag(self, image_id: int, tag_name: str):
        row = self.conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
        if not row:
            return {"ok": False, "error": "タグが見つかりません", "code": "tag_not_found"}
        with self._db_lock:
            self.conn.execute(
                "DELETE FROM image_tags WHERE image_id = ? AND tag_id = ?",
                (image_id, row["id"]),
            )
            self.conn.commit()
        return {"ok": True}

    AI_META_KEYS = ("parameters", "prompt", "workflow", "Comment", "Description", "Software", "software")

    def get_image_metadata(self, image_id: int):
        row = self.conn.execute("SELECT full_path FROM images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "画像が見つかりません", "code": "image_not_found"}
        try:
            with Image.open(self.root / row["full_path"]) as im:
                info = dict(im.info)
        except (OSError, UnidentifiedImageError) as e:
            return {"ok": False, "error": f"画像を読み込めませんでした: {e}", "code": "image_read_failed"}

        metadata = {}
        for key in self.AI_META_KEYS:
            val = info.get(key)
            if isinstance(val, bytes):
                val = val.decode("utf-8", errors="replace")
            if isinstance(val, str) and val.strip():
                metadata[key] = val
        return {"ok": True, "metadata": metadata}

    IMAGE_MIME_TYPES = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".gif": "image/gif",
    }

    def get_original_image(self, image_id: int):
        row = self.conn.execute("SELECT full_path FROM images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "画像が見つかりません", "code": "image_not_found"}
        path = self.root / row["full_path"]
        try:
            data = path.read_bytes()
        except OSError as e:
            return {"ok": False, "error": f"画像を読み込めませんでした: {e}", "code": "image_read_failed"}
        mime = self.IMAGE_MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
        data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        return {"ok": True, "data_url": data_url}

    # ---- filter presets ----

    def list_presets(self):
        if self.conn is None:
            return []
        rows = self.conn.execute(
            "SELECT id, name, tag_names, mode FROM presets ORDER BY created_at ASC"
        ).fetchall()
        return [
            {"id": r["id"], "name": r["name"], "tag_names": json.loads(r["tag_names"]), "mode": r["mode"]}
            for r in rows
        ]

    def save_preset(self, name: str, tag_names: list[str], mode: str = "and"):
        if self.conn is None:
            return self._no_library()
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "名前を入力してください", "code": "name_required"}
        if not tag_names:
            return {"ok": False, "error": "タグが選択されていません", "code": "tags_required"}
        cur = self.conn.execute(
            "INSERT INTO presets (name, tag_names, mode, created_at) VALUES (?, ?, ?, ?)",
            (name, json.dumps(tag_names, ensure_ascii=False), mode, db.now_iso()),
        )
        self.conn.commit()
        return {"ok": True, "id": cur.lastrowid}

    def delete_preset(self, preset_id: int):
        if self.conn is None:
            return self._no_library()
        self.conn.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
        self.conn.commit()
        return {"ok": True}

    # ---- tagging ----

    def start_tagging(self):
        if self.conn is None:
            return self._no_library()
        if self._tagging_state["running"]:
            return {"ok": False, "error": "すでに実行中です", "code": "already_running"}
        workers = _tagging_worker_count()
        self._tagging_state.update(
            running=True,
            total=0,
            done=0,
            current="",
            error=None,
            workers=workers,
            cancel_requested=False,
            cancelled=False,
        )
        thread = threading.Thread(target=self._tagging_worker, args=(workers,), daemon=True)
        thread.start()
        return {"ok": True, "workers": workers}

    def stop_tagging(self):
        if not self._tagging_state["running"]:
            return {"ok": False, "error": "タグ付けは実行されていません", "code": "not_running"}
        self._tagging_state["cancel_requested"] = True
        return {"ok": True}

    def get_tagging_progress(self):
        return dict(self._tagging_state)

    def _tagging_worker(self, workers: int):
        state = self._tagging_state
        state.update(running=True, total=0, done=0, current="", error=None, workers=workers)
        try:
            import tagger_florence
            import tagger_text_nlp

            with self._db_lock:
                # 画像の未処理分を取得 (florence2タグがないもの)
                image_rows = self.conn.execute(
                    """SELECT i.id, i.full_path FROM images i
                       WHERE i.media_type = 'image' AND NOT EXISTS (
                           SELECT 1 FROM image_tags it WHERE it.image_id = i.id AND it.source = 'florence2'
                       )"""
                ).fetchall()
                # テキストの未処理分を取得 (text_nlpタグがないもの)
                text_rows = self.conn.execute(
                    """SELECT i.id, i.full_path FROM images i
                       WHERE i.media_type = 'text' AND NOT EXISTS (
                           SELECT 1 FROM image_tags it WHERE it.image_id = i.id AND it.source = 'text_nlp'
                       )"""
                ).fetchall()

            image_targets = [(r["id"], self.root / r["full_path"]) for r in image_rows]
            text_targets = [(r["id"], self.root / r["full_path"]) for r in text_rows]
            
            state["total"] = len(image_targets) + len(text_targets)

            # 1. テキストファイルの処理（軽量なのでシングルスレッドで順次処理）
            for doc_id, doc_path in text_targets:
                if state["cancel_requested"]:
                    state["cancelled"] = True
                    break
                state["current"] = doc_path.name
                try:
                    tags = tagger_text_nlp.tag_document(doc_path)
                    self._save_tags(doc_id, tags, "text_nlp")
                except Exception:
                    traceback.print_exc()
                else:
                    state["done"] += 1

            if state["cancel_requested"]:
                state["cancelled"] = True

            # 2. 画像ファイルの処理 (既存の ProcessPoolExecutor)
            if image_targets and not state["cancel_requested"]:
                executor = ProcessPoolExecutor(max_workers=workers)
                pending = {}
                targets = iter(image_targets)
                try:
                    for image_id, path in list(image_targets)[:workers]:
                        pending[executor.submit(tagger_florence.tag_image, path)] = (image_id, path)
                    for _ in range(min(workers, len(image_targets))):
                        next(targets)

                    while pending:
                        completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                        for future in completed:
                            image_id, path = pending.pop(future)
                            state["current"] = path.name
                            try:
                                tags = future.result()
                            except Exception:
                                traceback.print_exc()
                            else:
                                self._save_tags(image_id, tags, "florence2")
                                state["done"] += 1

                            if state["cancel_requested"]:
                                state["cancelled"] = True
                                continue
                            try:
                                next_image_id, next_path = next(targets)
                            except StopIteration:
                                continue
                            pending[executor.submit(tagger_florence.tag_image, next_path)] = (
                                next_image_id,
                                next_path,
                            )

                        if state["cancel_requested"]:
                            for future in pending:
                                future.cancel()
                            break
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
        except Exception as e:
            traceback.print_exc()
            state["error"] = str(e)
        finally:
            if state["cancel_requested"]:
                state["cancelled"] = True
            state["running"] = False
            state["current"] = ""

    def _save_tags(self, image_id: int, tags: list[tuple[str, float]], source: str):
        with self._db_lock:
            for name, score in tags:
                cur = self.conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
                row = cur.fetchone()
                if row:
                    tag_id = row["id"]
                else:
                    cur = self.conn.execute(
                        "INSERT INTO tags (name, count, category) VALUES (?, 0, ?)", (name, get_category(name))
                    )
                    tag_id = cur.lastrowid
                self.conn.execute(
                    """INSERT OR IGNORE INTO image_tags (image_id, tag_id, source, score)
                       VALUES (?, ?, ?, ?)""",
                    (image_id, tag_id, source, score),
                )
                self.conn.execute("UPDATE tags SET count = count + 1 WHERE id = ?", (tag_id,))
            self.conn.commit()

    # ---- TagSpaces-style portable tags ----

    def embed_tags_to_filename(self, image_id: int):
        if self.conn is None:
            return self._no_library()
        row = self.conn.execute(
            "SELECT full_path, filename FROM images WHERE id = ?", (image_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "ファイルが見つかりません", "code": "file_not_found"}

        old_path = self.root / row["full_path"]
        if not old_path.exists():
            return {"ok": False, "error": "実ファイルが存在しません", "code": "file_missing"}

        # タグ一覧の取得
        tags = self.get_image_tags(image_id)
        if not tags:
            return {"ok": False, "error": "タグが設定されていません", "code": "no_tags"}

        tag_names = [t["name"] for t in tags]
        
        # 既存のブラケット [ ... ] 部分を取り除く
        clean_stem = re.sub(r"\s*\[[^\]]+\]", "", old_path.stem).strip()
        
        # 新しいファイル名の作成
        tags_suffix = " [" + " ".join(tag_names) + "]"
        new_filename = f"{clean_stem}{tags_suffix}{old_path.suffix}"
        
        # rename_image を再利用して安全にリネームを実行
        return self.rename_image(image_id, new_filename)

    def export_sidecar_json(self, image_id: int):
        if self.conn is None:
            return self._no_library()
        row = self.conn.execute(
            "SELECT full_path, media_type FROM images WHERE id = ?", (image_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "ファイルが見つかりません", "code": "file_not_found"}

        file_path = self.root / row["full_path"]
        if not file_path.exists():
            return {"ok": False, "error": "実ファイルが存在しません", "code": "file_missing"}

        tags = self.get_image_tags(image_id)
        meta_data = {
            "tags": [t["name"] for t in tags],
            "media_type": row["media_type"],
            "exported_at": db.now_iso()
        }

        # サイドカーファイルのパス (例: file.txt.json)
        sidecar_path = file_path.parent / f"{file_path.name}.json"
        try:
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump(meta_data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            return {"ok": False, "error": f"サイドカーファイルの作成に失敗しました: {e}", "code": "write_failed"}

        return {"ok": True, "sidecar_path": str(sidecar_path)}

    def get_document_text(self, image_id: int):
        if self.conn is None:
            return {"ok": False, "error": "Library not open", "code": "no_library"}
        row = self.conn.execute(
            "SELECT full_path FROM images WHERE id = ?", (image_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "ファイルが見つかりません", "code": "file_not_found"}
        
        path = self.root / row["full_path"]
        if not path.exists():
            return {"ok": False, "error": "実ファイルが存在しません", "code": "file_missing"}

        import tagger_text_nlp
        text = tagger_text_nlp.extract_text(path)
        return {"ok": True, "text": text}
