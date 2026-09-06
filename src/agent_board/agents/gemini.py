"""Gemini CLI 探测器。

进程匹配较宽松（gemini 也是常见词），结合 ~/.gemini 目录存在性共同判断；
最近活动取 ~/.gemini/tmp 下最新 mtime。
会话列表通过 `gemini --list-sessions`（按项目，这里固定在 ~）获取，
恢复用 `gemini --resume <序号>`。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .base import AgentStatus, ProcScan, newest_mtime

GEMINI_HOME = Path(os.path.expanduser("~/.gemini"))
SESSION_CWD = os.path.expanduser("~")   # --list-sessions / --resume 都按所在项目生效


def _match_procs(scan: ProcScan):
    procs = []
    for p in scan.procs:
        cl = " ".join(p.info.get("cmdline") or [])
        if not cl:
            continue
        low = cl.lower()
        if "gemini" in low and "chrome_crashpad" not in low:
            procs.append(p)
    return procs


def list_sessions(limit: int = 12, hidden_ids: frozenset | set = frozenset()) -> dict:
    """`gemini --list-sessions` 的解析结果。

    注意：--resume 按序号定位，序号会随会话增减漂移，隐藏/改名持久化的 id
    也基于该序号（当前本机尚无已存会话，等真实数据出现后再按实际输出细化）。
    """
    try:
        r = subprocess.run(["gemini", "--list-sessions"], capture_output=True, text=True,
                           timeout=30, cwd=SESSION_CWD, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {"visible": [], "hidden": []}
    visible, hidden = [], []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        m = re.match(r"^(\d{1,3})\s*[.):：]?\s+(.{2,})", line)   # 以序号开头的条目行
        if not m:
            continue
        idx, desc = m.group(1), m.group(2).strip()
        item = {"id": idx, "title": desc, "cwd": SESSION_CWD,
                "last_active": None, "running": False}
        if idx in hidden_ids:
            hidden.append(item)
        else:
            visible.append(item)
    return {"visible": visible[:limit], "hidden": hidden[:50]}


def probe(scan: ProcScan) -> list[AgentStatus]:
    procs = _match_procs(scan)
    newest = None
    tmp = GEMINI_HOME / "tmp"
    if tmp.is_dir():
        newest = newest_mtime([p for p in tmp.iterdir()])
    installed = shutil.which("gemini") is not None or GEMINI_HOME.is_dir()

    if not procs:
        return [AgentStatus(
            id="gemini", name="Gemini CLI", kind="cli", state="offline",
            activity="未安装" if not installed else "未运行",
            last_active=newest,
            enter={"type": "iterm", "needle": "gemini"},
            has_sessions=True,
        )]

    cpu, mem = scan.usage(procs)
    return [AgentStatus(
        id="gemini", name="Gemini CLI", kind="cli",
        state="running",
        activity="会话运行中",
        last_active=newest,
        pid=procs[0].info["pid"],
        cpu_percent=cpu or None,
        mem_mb=mem or None,
        enter={"type": "iterm", "needle": "gemini"},
        has_sessions=True,
    )]
