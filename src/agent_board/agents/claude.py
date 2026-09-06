"""Claude Code CLI 探测器。

实时状态权威来源：~/.claude/sessions/<pid>.json
字段实测：pid / sessionId / cwd / status(idle|...) / statusUpdatedAt /
updatedAt / name / kind / entrypoint / version / startedAt（时间均为 epoch 毫秒）。
会话列表来源：~/.claude/projects/*/<uuid>.jsonl（行内 cwd + ai-title，只读元数据）。
"""
from __future__ import annotations

import glob
import json
import os
import time

import psutil

from .base import AgentStatus, ProcScan, ms_to_ts, today_start

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
TAIL_BYTES = 32_000   # 每个会话文件只读末尾 32KB + 开头 8KB，避免读入对话内容大头
HEAD_BYTES = 8_000


def _tail_lines(path: str, nbytes: int) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        data = f.read(nbytes).decode("utf-8", "replace")
    lines = data.splitlines()
    if size > nbytes and lines:
        lines = lines[1:]  # 丢弃可能被截断的首行
    return lines


def _extract_meta(path: str) -> tuple[str | None, str | None]:
    """从 jsonl 的元数据行里取 (cwd, title)，不解析 message 内容。"""
    cwd = title = None
    for line in _tail_lines(path, TAIL_BYTES):
        if cwd and title:
            break
        if cwd is None and '"cwd"' in line:
            try:
                cwd = json.loads(line).get("cwd")
            except ValueError:
                pass
        if title is None and '"ai-title"' in line:
            try:
                d = json.loads(line)
                if d.get("type") == "ai-title":
                    title = d.get("aiTitle") or d.get("title")  # 实测字段为 aiTitle
            except ValueError:
                pass
    if title is None:  # ai-title 可能生成在文件开头
        for line in _tail_lines(path, HEAD_BYTES):
            if '"ai-title"' in line:
                try:
                    d = json.loads(line)
                    if d.get("type") == "ai-title":
                        title = d.get("aiTitle") or d.get("title")
                except ValueError:
                    pass
    return cwd, title


def _live_session_ids() -> set[str]:
    """运行中 claude 进程的 sessionId 集合。"""
    out = set()
    for fn in os.listdir(SESSIONS_DIR) if os.path.isdir(SESSIONS_DIR) else []:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if d.get("sessionId") and psutil.pid_exists(d.get("pid")):
            out.add(d["sessionId"])
    return out


def _session_from_path(path: str, live: set[str]) -> dict | None:
    sid = os.path.splitext(os.path.basename(path))[0]
    try:
        cwd, title = _extract_meta(path)
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    home = os.path.expanduser("~")
    return {"id": sid, "title": title, "cwd": cwd if cwd and os.path.isdir(cwd) else home,
            "last_active": mtime, "running": sid in live}


def live_session_ids() -> set:
    return _live_session_ids()


def list_sessions(limit: int = 12, hidden_ids: frozenset | set = frozenset()) -> dict:
    """可见 + 被隐藏的最近会话（跨项目），供看板「会话」弹窗使用。

    会话 id 即文件名：隐藏集合按文件名筛选即可识别，只对选中的文件读元数据。
    """
    files = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))

    def _mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    files.sort(key=_mtime, reverse=True)
    sel_visible, sel_hidden = [], []
    for path in files:
        sid = os.path.splitext(os.path.basename(path))[0]
        if sid in hidden_ids:
            if len(sel_hidden) < 50:
                sel_hidden.append(path)
        elif len(sel_visible) < limit:
            sel_visible.append(path)
        if len(sel_visible) >= limit and len(sel_hidden) >= 50:
            break

    live = _live_session_ids()
    visible = [s for p in sel_visible if (s := _session_from_path(p, live))]
    hidden = [s for p in sel_hidden if (s := _session_from_path(p, live))]
    hidden.sort(key=lambda s: s["last_active"], reverse=True)
    return {"visible": visible, "hidden": hidden}


_STATS = {"ts": 0.0, "data": {}}
_MODEL = {"path": "", "mtime": 0.0, "model": None}


def collect_stats() -> dict:
    """身份/统计行数据（15s 缓存）：model / today_sessions。"""
    now = time.time()
    if now - _STATS["ts"] < 15:
        return _STATS["data"]
    data = {}
    files = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    today = today_start()
    data["today_sessions"] = sum(1 for p in files
                                 if os.path.exists(p) and os.path.getmtime(p) >= today)
    # 并行会话数：sessions/ 下 pid 仍存活的实时状态文件数
    live = 0
    if os.path.isdir(SESSIONS_DIR):
        for fn in os.listdir(SESSIONS_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(SESSIONS_DIR, fn), encoding="utf-8") as f:
                    if psutil.pid_exists(json.load(f).get("pid")):
                        live += 1
            except (OSError, ValueError):
                continue
    if live > 1:
        data["parallel_sessions"] = live
    latest = max(files, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                 default=None)
    if latest:
        try:
            mtime = os.path.getmtime(latest)
            if _MODEL["path"] != latest or _MODEL["mtime"] != mtime:
                model = None
                for line in _tail_lines(latest, 32_000):   # 只取元数据字段
                    if '"model"' in line:
                        try:
                            d = json.loads(line)
                            msg = d.get("message")
                            m = msg.get("model") if isinstance(msg, dict) else None
                            model = m or d.get("model") or model
                        except ValueError:
                            pass
                _MODEL.update(path=latest, mtime=mtime, model=model)
            if _MODEL["model"]:
                data["model"] = _MODEL["model"]
        except OSError:
            pass
    _STATS.update(ts=now, data=data)
    return data


def probe(scan: ProcScan) -> list[AgentStatus]:
    if not os.path.isdir(SESSIONS_DIR):
        return []
    sessions = []
    for fn in os.listdir(SESSIONS_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fn), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        pid = data.get("pid")
        if not pid or not psutil.pid_exists(pid):   # 跳过陈旧文件
            continue
        sessions.append(data)

    if not sessions:
        return []

    sessions.sort(key=lambda d: d.get("updatedAt") or 0, reverse=True)
    latest = sessions[0]
    cwd = latest.get("cwd") or ""
    project = os.path.basename(cwd) if cwd else None
    status = (latest.get("status") or "").lower()
    state = "idle" if status == "idle" else "busy" if status else "running"

    cpu, mem = scan.usage([p for p in scan.procs if p.info["pid"] == latest.get("pid")])

    others = len(sessions) - 1
    return [AgentStatus(
        id="claude",
        name="Claude Code",
        kind="cli",
        state=state,
        activity=("执行任务中" if state == "busy" else "空闲等待输入") +
                 (f"（另有 {others} 个会话）" if others else ""),
        project=project,
        title=latest.get("name") or (latest.get("sessionId") or "")[:8] or None,
        model=latest.get("model"),
        last_active=ms_to_ts(latest.get("updatedAt")),
        pid=latest.get("pid"),
        cpu_percent=cpu or None,
        mem_mb=mem or None,
        enter={"type": "iterm", "needle": project or ""},
        has_sessions=True,
        stats=collect_stats(),
        detail={"version": latest.get("version"),
                "sessions": [s.get("name") or (s.get("sessionId") or "")[:8] for s in sessions]},
    )]
