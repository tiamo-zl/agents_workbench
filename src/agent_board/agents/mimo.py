"""Mimo Code CLI 探测器（mimocode，opencode 系）。

数据源：
- 进程：~/.mimocode/bin/mimo（参数精确匹配，避免误伤其它含 "mimo" 字样的进程）。
- 会话/活动：~/.local/share/mimocode/mimocode.db（只读 mode=ro）
  session 表 id / title / directory / time_updated（epoch 毫秒），parent_id
  非空的为子会话，不列出。
恢复：`mimo -s <sessionID>`；无头任务：`mimo run <msg>`。
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from .base import AgentStatus, ProcScan, ms_to_ts, today_start

MIMO_DATA = Path(os.path.expanduser("~/.local/share/mimocode"))
MIMO_DB = MIMO_DATA / "mimocode.db"
RECENT_WINDOW = 120


def _match_procs(scan: ProcScan):
    out = []
    for p in scan.procs:
        args = [a for a in (p.info.get("cmdline") or []) if a]
        if any(a == "mimo" or a.endswith("/mimo") or "mimocode" in a for a in args):
            out.append(p)
    return out


def _db_mtime() -> float | None:
    try:
        return MIMO_DB.stat().st_mtime
    except OSError:
        return None


def list_sessions(limit: int = 12, hidden_ids: frozenset | set = frozenset()) -> dict:
    if not MIMO_DB.exists():
        return {"visible": [], "hidden": []}
    rows = []
    try:
        conn = sqlite3.connect(f"file:{MIMO_DB}?mode=ro", uri=True, timeout=1)
        try:
            rows = conn.execute(
                "SELECT id, title, directory, time_updated FROM session "
                "WHERE parent_id IS NULL ORDER BY time_updated DESC"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {"visible": [], "hidden": []}

    home = os.path.expanduser("~")
    visible, hidden = [], []
    for sid, title, directory, updated in rows:
        item = {"id": sid, "title": (title or "").splitlines()[0] if title else None,
                "cwd": directory if directory and os.path.isdir(directory) else home,
                "last_active": ms_to_ts(updated), "running": False}
        if sid in hidden_ids:
            if len(hidden) < 50:
                hidden.append(item)
        else:
            visible.append(item)
        if len(visible) >= limit and len(hidden) >= 50:
            break
    return {"visible": visible[:limit], "hidden": hidden}


_STATS = {"ts": 0.0, "data": {}}


def collect_stats() -> dict:
    """今日/总会话数、版本、最近会话的代码增删（15s 缓存）。"""
    now = time.time()
    if now - _STATS["ts"] < 15:
        return _STATS["data"]
    data = {}
    if MIMO_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{MIMO_DB}?mode=ro", uri=True, timeout=1)
            try:
                today_ms = int(today_start() * 1000)
                total, today = conn.execute(
                    "SELECT count(*), sum(time_updated >= ?) FROM session",
                    (today_ms,)).fetchone()
                data["total_sessions"], data["today_sessions"] = total or 0, today or 0
                row = conn.execute(
                    "SELECT version, summary_additions, summary_deletions FROM session "
                    "WHERE parent_id IS NULL ORDER BY time_updated DESC LIMIT 1").fetchone()
                if row:
                    if row[0]:
                        data["version"] = row[0]
                    if row[1] or row[2]:
                        data["add"], data["del"] = row[1] or 0, row[2] or 0
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    _STATS.update(ts=now, data=data)
    return data


def probe(scan: ProcScan) -> AgentStatus:
    procs = _match_procs(scan)
    db_ts = _db_mtime()
    cpu, mem = scan.usage(procs) if procs else (0.0, 0.0)
    enter = {"type": "iterm", "needle": "mimo"}

    if not procs:
        return AgentStatus(id="mimo", name="Mimo Code", kind="cli", state="offline",
                           activity="未运行", last_active=db_ts,
                           enter=enter, has_sessions=True, stats=collect_stats())

    active = db_ts is not None and (time.time() - db_ts) < RECENT_WINDOW
    return AgentStatus(id="mimo", name="Mimo Code", kind="cli",
                       state="busy" if active else "idle",
                       activity="正在执行任务" if active else "空闲等待输入",
                       last_active=db_ts, pid=procs[0].info["pid"],
                       cpu_percent=cpu or None, mem_mb=mem or None,
                       enter=enter, has_sessions=True, stats=collect_stats())
