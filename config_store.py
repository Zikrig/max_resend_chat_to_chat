"""
SQLite v4: множество пар зеркал A→B и глобальные правила подмены строк.
При несовпадении версии схемы (кроме v3→v4) файл БД пересоздаётся.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "4"
BACKUP_RETENTION_SEC = 7 * 24 * 3600

DEFAULT_REPLACEMENTS: List[Tuple[str, str]] = [("vk.com", "ya.ru")]


def ensure_dir(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def _parent_dir(path: str) -> str:
    return os.path.dirname(os.path.abspath(path))


def _connect(db_path: str) -> sqlite3.Connection:
    parent = _parent_dir(db_path)
    if parent:
        ensure_dir(parent)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _read_schema_version(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row else None


def _drop_legacy_tables(conn: sqlite3.Connection) -> None:
    for name in (
        "tracked_posts",
        "channel_bindings",
        "users",
        "app_config",
        "settings",
        "mirror_pairs",
        "replacements",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {name}")


def _init_schema_v4(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mirror_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_chat_id INTEGER,
            target_chat_id INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS replacements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_text TEXT NOT NULL,
            replace_text TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
        (SCHEMA_VERSION,),
    )


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    source: Optional[int] = None
    target: Optional[int] = None
    try:
        srow = conn.execute(
            "SELECT source_chat_id, target_chat_id FROM settings WHERE id = 1"
        ).fetchone()
        if srow:
            if srow["source_chat_id"] is not None:
                source = int(srow["source_chat_id"])
            if srow["target_chat_id"] is not None:
                target = int(srow["target_chat_id"])
    except sqlite3.OperationalError:
        pass

    conn.execute("DROP TABLE IF EXISTS settings")
    _init_schema_v4(conn)

    if source is not None or target is not None:
        conn.execute(
            """INSERT INTO mirror_pairs (source_chat_id, target_chat_id, enabled, sort_order)
               VALUES (?, ?, 1, 0)""",
            (source, target),
        )
        logger.info("Миграция v3→v4: перенесена одна пара source=%s target=%s", source, target)


def _seed_defaults(conn: sqlite3.Connection) -> None:
    cnt = conn.execute("SELECT COUNT(*) FROM replacements").fetchone()[0]
    if cnt == 0:
        for i, (search, replace) in enumerate(DEFAULT_REPLACEMENTS):
            conn.execute(
                """INSERT INTO replacements (search_text, replace_text, enabled, sort_order)
                   VALUES (?, ?, 1, ?)""",
                (search, replace, i),
            )


def _recreate_db_file(db_path: str) -> None:
    if os.path.isfile(db_path):
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{db_path}.legacy-{ts}.bak"
        try:
            os.replace(db_path, backup)
            logger.warning("Старая БД переименована в %s, создаётся схема v4", backup)
        except OSError:
            os.remove(db_path)
            logger.warning("Старая БД удалена, создаётся схема v4")


def _row_to_pair(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "source_chat_id": int(row["source_chat_id"]) if row["source_chat_id"] is not None else None,
        "target_chat_id": int(row["target_chat_id"]) if row["target_chat_id"] is not None else None,
        "enabled": bool(row["enabled"]),
        "sort_order": int(row["sort_order"]),
    }


def open_db(db_path: str) -> sqlite3.Connection:
    """Открывает БД v4; v3 мигрирует in-place, более старые версии пересоздаёт."""
    if os.path.isfile(db_path):
        conn = _connect(db_path)
        ver = _read_schema_version(conn)
        conn.close()
        if ver is not None and ver not in (SCHEMA_VERSION, "3"):
            _recreate_db_file(db_path)
    conn = _connect(db_path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        ver = _read_schema_version(conn)
        if ver == "3":
            _migrate_v3_to_v4(conn)
            _seed_defaults(conn)
        elif ver != SCHEMA_VERSION:
            _drop_legacy_tables(conn)
            _init_schema_v4(conn)
            _seed_defaults(conn)
        else:
            _init_schema_v4(conn)
            _seed_defaults(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return conn


def _load_pairs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    pairs: List[Dict[str, Any]] = []
    for row in conn.execute(
        "SELECT id, source_chat_id, target_chat_id, enabled, sort_order "
        "FROM mirror_pairs ORDER BY sort_order, id"
    ):
        pairs.append(_row_to_pair(row))
    return pairs


def _load_replacements(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    replacements: List[Dict[str, Any]] = []
    for r in conn.execute(
        "SELECT id, search_text, replace_text, enabled, sort_order FROM replacements ORDER BY sort_order, id"
    ):
        replacements.append(
            {
                "id": int(r["id"]),
                "search_text": str(r["search_text"]),
                "replace_text": str(r["replace_text"]),
                "enabled": bool(r["enabled"]),
                "sort_order": int(r["sort_order"]),
            }
        )
    return replacements


def load_state(db_path: str) -> Dict[str, Any]:
    conn = open_db(db_path)
    try:
        return {
            "pairs": _load_pairs(conn),
            "replacements": _load_replacements(conn),
        }
    finally:
        conn.close()


def get_pair(db_path: str, pair_id: int) -> Optional[Dict[str, Any]]:
    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT id, source_chat_id, target_chat_id, enabled, sort_order FROM mirror_pairs WHERE id = ?",
            (pair_id,),
        ).fetchone()
        return _row_to_pair(row) if row else None
    finally:
        conn.close()


def create_pair(db_path: str) -> int:
    conn = open_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        mx = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM mirror_pairs").fetchone()[0]
        cur = conn.execute(
            """INSERT INTO mirror_pairs (source_chat_id, target_chat_id, enabled, sort_order)
               VALUES (NULL, NULL, 1, ?)""",
            (int(mx) + 1,),
        )
        conn.commit()
        return int(cur.lastrowid)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_pair_chats(
    db_path: str,
    pair_id: int,
    *,
    source_chat_id: Optional[int] = None,
    target_chat_id: Optional[int] = None,
) -> bool:
    conn = open_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT id FROM mirror_pairs WHERE id = ?", (pair_id,)).fetchone()
        if not row:
            conn.rollback()
            return False
        if source_chat_id is not None:
            conn.execute(
                "UPDATE mirror_pairs SET source_chat_id = ? WHERE id = ?",
                (source_chat_id, pair_id),
            )
        if target_chat_id is not None:
            conn.execute(
                "UPDATE mirror_pairs SET target_chat_id = ? WHERE id = ?",
                (target_chat_id, pair_id),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_pair(db_path: str, pair_id: int) -> bool:
    conn = open_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM mirror_pairs WHERE id = ?", (pair_id,))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def toggle_pair(db_path: str, pair_id: int) -> bool:
    conn = open_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT enabled FROM mirror_pairs WHERE id = ?", (pair_id,)).fetchone()
        if not row:
            conn.rollback()
            return False
        new_val = 0 if row["enabled"] else 1
        conn.execute("UPDATE mirror_pairs SET enabled = ? WHERE id = ?", (new_val, pair_id))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_enabled_replacement_rules(db_path: str) -> List[Tuple[str, str]]:
    state = load_state(db_path)
    return [
        (r["search_text"], r["replace_text"])
        for r in state["replacements"]
        if r["enabled"] and r["search_text"]
    ]


def add_replacement(db_path: str, search_text: str, replace_text: str) -> int:
    conn = open_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        mx = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM replacements").fetchone()[0]
        cur = conn.execute(
            """INSERT INTO replacements (search_text, replace_text, enabled, sort_order)
               VALUES (?, ?, 1, ?)""",
            (search_text, replace_text, int(mx) + 1),
        )
        conn.commit()
        return int(cur.lastrowid)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def toggle_replacement(db_path: str, repl_id: int) -> bool:
    conn = open_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT enabled FROM replacements WHERE id = ?", (repl_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        new_val = 0 if row["enabled"] else 1
        conn.execute("UPDATE replacements SET enabled = ? WHERE id = ?", (new_val, repl_id))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_replacement(db_path: str, repl_id: int) -> bool:
    conn = open_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM replacements WHERE id = ?", (repl_id,))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def dump_database_to_sql(db_path: str, out_sql: str) -> None:
    if not os.path.isfile(db_path):
        logger.warning("Дамп: файл БД не найден: %s", db_path)
        return
    parent = _parent_dir(out_sql)
    if parent:
        ensure_dir(parent)
    conn = sqlite3.connect(db_path)
    try:
        with open(out_sql, "w", encoding="utf-8") as f:
            for line in conn.iterdump():
                f.write(line)
                if not line.endswith("\n"):
                    f.write("\n")
    finally:
        conn.close()


def prune_old_backups(backup_dir: str, max_age_sec: float = BACKUP_RETENTION_SEC) -> None:
    if not os.path.isdir(backup_dir):
        return
    cutoff = time.time() - max_age_sec
    for name in os.listdir(backup_dir):
        path = os.path.join(backup_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                logger.info("Удалён старый дамп: %s", path)
        except OSError as e:
            logger.warning("Не удалось удалить %s: %s", path, e)


def backup_now(db_path: str, backup_dir: str) -> Optional[str]:
    ensure_dir(backup_dir)
    prune_old_backups(backup_dir)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(backup_dir, f"app-{ts}.sql")
    dump_database_to_sql(db_path, out_path)
    logger.info("Дамп SQLite: %s", out_path)
    return out_path
