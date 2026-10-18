#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""subhub SQLite 存储层。表结构见 init_db 注释。"""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get(
    "SUBHUB_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "subhub.db"))

SCHEMA = """
-- 订阅源。kind: manual(手动加) / feed(固定资源同步来); origin: 来源 feed 名
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, url TEXT UNIQUE, kind TEXT DEFAULT 'manual', origin TEXT,
    enabled INTEGER DEFAULT 1,
    degraded INTEGER DEFAULT 0,          -- 连续失败自动降级标记(不删, 只警示)
    user_ok INTEGER DEFAULT 0,           -- 用户在 App 里验证过可用: 覆盖 vantage 结论
    fail_streak INTEGER DEFAULT 0,
    created_at INTEGER, updated_at INTEGER
);
-- 每次检测记录。ok: 1/0; verdict: 完整结论串; detail: JSON
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER, ts INTEGER, ok INTEGER, http TEXT,
    verdict TEXT, sites INTEGER, lives INTEGER, jar TEXT, note TEXT,
    ms INTEGER, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_src ON checks(source_id, ts);
-- 固定资源(父订阅页): 定时拉取 → 自动发现新订阅
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, url TEXT UNIQUE, enabled INTEGER DEFAULT 1,
    last_sync INTEGER, last_found INTEGER DEFAULT 0, last_error TEXT
);
-- 设置项
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
-- 运行日志
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, msg TEXT
);
"""

_lock = threading.Lock()
_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        # 迁移: sort_order 列(老库没有); 默认按 id 铺底, 新源 append 到队尾
        cols = [r[1] for r in _conn.execute("PRAGMA table_info(sources)").fetchall()]
        if "sort_order" not in cols:
            _conn.execute("ALTER TABLE sources ADD COLUMN sort_order INTEGER DEFAULT 0")
            _conn.execute("UPDATE sources SET sort_order = id")
        if "user_ok" not in cols:  # 老库无此列
            _conn.execute("ALTER TABLE sources ADD COLUMN user_ok INTEGER DEFAULT 0")
        defaults = {"interval_minutes": "10", "check_timeout": "12",
                    "deep_check": "1", "max_workers": "8",
                    "merge_cap": "50", "include_degraded": "0",
                    "single_master": "0", "single_lives": "1",
                    "single_parses": "1"}
        for k, v in defaults.items():
            _conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        _conn.commit()
    return _conn


def now():
    return int(time.time())


# ---------- settings ----------
def get_settings():
    rows = conn().execute("SELECT key,value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_setting(key, value):
    with _lock:
        conn().execute("INSERT INTO settings(key,value) VALUES(?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, str(value)))
        conn().commit()


# ---------- sources ----------
def add_source(name, url, kind="manual", origin=None):
    url = url.strip()
    with _lock:
        nxt = conn().execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM sources").fetchone()[0]
        cur = conn().execute(
            "INSERT OR IGNORE INTO sources(name,url,kind,origin,created_at,updated_at,sort_order)"
            " VALUES(?,?,?,?,?,?,?)", (name, url, kind, origin, now(), now(), nxt))
        conn().commit()
        return cur.lastrowid


def ordered_sources():
    return [dict(r) for r in conn().execute(
        "SELECT * FROM sources ORDER BY sort_order, id").fetchall()]


def move_source(sid, direction):
    """与相邻源交换 sort_order。direction: -1 上移 / +1 下移"""
    srcs = ordered_sources()
    idx = next((i for i, s in enumerate(srcs) if s["id"] == sid), None)
    if idx is None:
        return False
    j = idx + direction
    if j < 0 or j >= len(srcs):
        return False
    a, b = srcs[idx], srcs[j]
    with _lock:
        conn().execute("UPDATE sources SET sort_order=? WHERE id=?",
                       (b["sort_order"], a["id"]))
        conn().execute("UPDATE sources SET sort_order=? WHERE id=?",
                       (a["sort_order"], b["id"]))
        conn().commit()
    return True


def list_sources(include_disabled=True):
    sql = "SELECT * FROM sources" if include_disabled else \
          "SELECT * FROM sources WHERE enabled=1"
    return [dict(r) for r in conn().execute(
        sql + " ORDER BY sort_order, id").fetchall()]


def get_source(sid):
    r = conn().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def update_source(sid, **fields):
    if not fields:
        return
    fields["updated_at"] = now()
    sql = "UPDATE sources SET " + ",".join("%s=?" % k for k in fields) + " WHERE id=?"
    with _lock:
        conn().execute(sql, list(fields.values()) + [sid])
        conn().commit()


def delete_source(sid):
    with _lock:
        conn().execute("DELETE FROM sources WHERE id=?", (sid,))
        conn().execute("DELETE FROM checks WHERE source_id=?", (sid,))
        conn().commit()


# ---------- checks ----------
def add_check(source_id, r):
    with _lock:
        conn().execute(
            "INSERT INTO checks(source_id,ts,ok,http,verdict,sites,lives,jar,note,ms,detail)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, now(), 1 if r["verdict"].startswith("✓") else 0,
             str(r.get("http")), r.get("verdict", ""), r.get("sites"),
             r.get("lives"), r.get("jar"), r.get("note", ""), r.get("ms"),
             json.dumps(r, ensure_ascii=False)))
        conn().commit()


def last_checks():
    """每个源最近 30 次检测(新→旧)"""
    rows = conn().execute(
        "SELECT * FROM checks ORDER BY id DESC LIMIT 3000").fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        out.setdefault(d["source_id"], [])
        if len(out[d["source_id"]]) < 30:
            out[d["source_id"]].append(d)
    return out


def clean_checks(keep_per_source=50):
    # 硬删不是软删: 检测记录是纯历史, 每源只留最近 keep_per_source 条
    with _lock:
        for s in list_sources():
            conn().execute(
                "DELETE FROM checks WHERE source_id=? AND id NOT IN ("
                "  SELECT id FROM checks WHERE source_id=? ORDER BY id DESC LIMIT ?)",
                (s["id"], s["id"], keep_per_source))
        conn().commit()


# ---------- feeds ----------
def list_feeds():
    return [dict(r) for r in conn().execute("SELECT * FROM feeds ORDER BY id").fetchall()]


def add_feed(name, url):
    with _lock:
        conn().execute("INSERT OR IGNORE INTO feeds(name,url) VALUES(?,?)",
                       (name, url.strip()))
        conn().commit()


def delete_feed(fid):
    with _lock:
        conn().execute("DELETE FROM feeds WHERE id=?", (fid,))
        conn().commit()


def update_feed(fid, **fields):
    fields["last_sync"] = fields.get("last_sync", now())
    sql = "UPDATE feeds SET " + ",".join("%s=?" % k for k in fields) + " WHERE id=?"
    with _lock:
        conn().execute(sql, list(fields.values()) + [fid])
        conn().commit()


# ---------- logs ----------
def log(level, msg):
    with _lock:
        conn().execute("INSERT INTO logs(ts,level,msg) VALUES(?,?,?)",
                       (now(), level, msg))
        conn().execute("DELETE FROM logs WHERE id < (SELECT MAX(id) FROM logs)-500")
        conn().commit()


def recent_logs(limit=100):
    return [dict(r) for r in conn().execute(
        "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
