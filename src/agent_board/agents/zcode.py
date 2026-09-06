"""ZCode 桌面应用探测器。

数据源：
- 进程：/Applications/ZCode.app 下任意进程存在即视为运行。
- ~/.zcode/cli/db/db.sqlite（只读 mode=ro）session 表：
  title / directory / time_updated（epoch 毫秒）/ task_type。
"""
from __future__ import annotations

import os
import sqlite3
import time

from .base import AgentStatus, ProcScan, ms_to_ts, today_start

DB_PATH = os.path.expanduser("~/.zcode/cli/db/db.sqlite")
RECENT_WINDOW = 120


def _latest_session() -> dict | None:
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute(
                "SELECT title, directory, time_updated FROM session "
                "WHERE time_archived IS NULL ORDER BY time_updated DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return {"title": row[0], "directory": row[1], "time_updated": row[2]}


_STATS = {"ts": 0.0, "data": {}}


def collect_stats() -> dict:
    """总/今日会话数、版本、最近会话的代码增删（15s 缓存，只读库）。"""
    now = time.time()
    if now - _STATS["ts"] < 15:
        return _STATS["data"]
    data = {}
    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=1)
            try:
                today_ms = int(today_start() * 1000)
                total, today = conn.execute(
                    "SELECT count(*), sum(time_updated >= ?) FROM session",
                    (today_ms,)).fetchone()
                data["total_sessions"], data["today_sessions"] = total or 0, today or 0
                row = conn.execute(
                    "SELECT version, summary_additions, summary_deletions FROM session "
                    "WHERE time_archived IS NULL ORDER BY time_updated DESC LIMIT 1").fetchone()
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


def probe(scan: ProcScan) -> list[AgentStatus]:
    procs = scan.with_exe_path("ZCode.app")
    session = _latest_session()
    cpu, mem = scan.usage(procs) if procs else (0.0, 0.0)

    if not procs:
        return [AgentStatus(
            id="zcode", name="ZCode", kind="app", state="offline",
            activity="未运行",
            title=session and session["title"],
            last_active=ms_to_ts(session and session.get("time_updated")),
            enter={"type": "app", "app": "ZCode"},
            stats=collect_stats(),
        )]

    updated = ms_to_ts(session and session.get("time_updated"))
    active = updated is not None and (time.time() - updated) < RECENT_WINDOW
    return [AgentStatus(
        id="zcode", name="ZCode", kind="app",
        state="busy" if active else "running",
        activity="会话处理中" if active else "运行中",
        project=os.path.basename(session["directory"]) if session and session.get("directory") else None,
        title=session and session["title"],
        last_active=updated,
        cpu_percent=cpu or None,
        mem_mb=mem or None,
        enter={"type": "app", "app": "ZCode"},
        stats=collect_stats(),
    )]
