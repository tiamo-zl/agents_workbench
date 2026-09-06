"""Codex CLI 探测器。

数据源：
- 进程表：交互式 codex（resume/exec/tty），排除 app-server（那是 ZCode 等宿主拉起的后端）与 codex-acp 桥。
- ~/.codex/session_index.jsonl：每行 {id, thread_name, updated_at}，取最新作标题。
- ~/.codex/thread-writer-locks/*.lock 与当天 rollout 文件 mtime 判断活跃。
注意：~/.codex/config.toml 含明文 token，本探测器绝不读取该文件。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from .base import AgentStatus, ProcScan, newest_mtime, today_start

CODEX_HOME = Path(os.path.expanduser("~/.codex"))
RECENT_WINDOW = 120  # 秒：有写入视为忙碌


def _interactive_codex(scan: ProcScan):
    procs = scan.with_cmdline("codex", exclude=("app-server", "codex-acp", "codex.js app-server"))
    return [p for p in procs if "app-server" not in " ".join(p.info.get("cmdline") or [])]


def _latest_title() -> tuple[str | None, float | None]:
    idx = CODEX_HOME / "session_index.jsonl"
    best_name, best_ts = None, None
    try:
        with open(idx, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                ts = row.get("updated_at")
                if ts:
                    try:
                        sec = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        sec = None
                    if sec and (best_ts is None or sec > best_ts):
                        best_ts, best_name = sec, row.get("thread_name")
    except OSError:
        pass
    return best_name, best_ts


def _recent_write_mtime() -> float | None:
    """最近被写入的会话数据：thread-writer-locks + 今天/昨天的 rollout 文件。"""
    candidates: list[Path] = []
    lock_dir = CODEX_HOME / "thread-writer-locks"
    if lock_dir.is_dir():
        candidates += [p for p in lock_dir.iterdir() if p.name != ".coordination.lock"]
    sess = CODEX_HOME / "sessions"
    if sess.is_dir():
        now = datetime.now()
        for day in (now, now - timedelta(days=1)):
            d = sess / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
            if d.is_dir():
                candidates += list(d.glob("*.jsonl"))
    return newest_mtime(candidates)


_STATS = {"ts": 0.0, "data": {}}
_RC = {"key": None, "model": None, "version": None, "tokens": None}


def collect_stats() -> dict:
    """身份/统计行数据（15s 缓存）：model_provider / cli_version / 今日会话数 / token。"""
    now = time.time()
    if now - _STATS["ts"] < 15:
        return _STATS["data"]
    data = {}
    day_dir = CODEX_HOME / "sessions" / datetime.now().strftime("%Y/%m/%d")
    rolls = sorted(day_dir.glob("rollout-*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True) if day_dir.is_dir() else []
    data["today_sessions"] = len(rolls)
    if rolls:
        r = rolls[0]
        try:
            key = (str(r), r.stat().st_size)
            if _RC["key"] != key:   # 文件没增长就沿用上次的解析结果
                model = version = tokens = ctxw = None
                with open(r, "rb") as f:
                    p = (json.loads(f.readline().decode("utf-8", "replace")).get("payload") or {})
                    model, version = p.get("model_provider"), p.get("cli_version")
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 65536))
                    for line in f.read().decode("utf-8", "replace").splitlines():
                        if '"token_count"' not in line:
                            continue
                        try:
                            d = json.loads(line)
                            info = (d.get("payload") or {}).get("info") or {}
                            t = info.get("total_token_usage") or {}
                            if t.get("total_tokens"):
                                tokens = t["total_tokens"]
                            if info.get("model_context_window"):
                                ctxw = info["model_context_window"]
                        except ValueError:
                            pass
                _RC.update(key=key, model=model, version=version, tokens=tokens, ctxw=ctxw)
            if _RC["model"]:
                data["model"] = _RC["model"]
            if _RC["version"]:
                data["version"] = _RC["version"]
            if _RC["tokens"]:
                data["total_tokens"] = _RC["tokens"]
            if _RC["tokens"] and _RC["ctxw"]:
                data["ctx_used"] = min(100, round(_RC["tokens"] / _RC["ctxw"] * 100))
        except (OSError, ValueError):
            pass
    _STATS.update(ts=now, data=data)
    return data


def _row_session(r: dict, live: set[str], home: str) -> dict | None:
    """索引行 → 会话条目；cwd 从对应 rollout 首行的 session_meta 解析。"""
    sid, updated = r.get("id"), r.get("updated_at") or ""
    cwd = None
    try:
        y, m, d = updated[:10].split("-")
        day_dir = CODEX_HOME / "sessions" / y / m / d
        matches = list(day_dir.glob(f"*{sid}.jsonl")) if day_dir.is_dir() else []
        if matches:
            with open(matches[0], encoding="utf-8") as f:
                meta = json.loads(f.readline())
            cwd = (meta.get("payload") or {}).get("cwd")
    except (OSError, ValueError):
        cwd = None
    ts = None
    try:
        ts = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    return {"id": sid, "title": r.get("thread_name"),
            "cwd": cwd if cwd and os.path.isdir(cwd) else home,
            "last_active": ts, "running": sid in live}


def list_sessions(limit: int = 12, hidden_ids: frozenset | set = frozenset()) -> dict:
    """可见 + 被隐藏的最近会话；标题/时间来自 session_index（只读元数据）。"""
    rows = []
    try:
        with open(CODEX_HOME / "session_index.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        return {"visible": [], "hidden": []}
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)

    # 先按 id 选出两列表的行，再解析元数据（避免为不展示的行打开 rollout）
    sel_visible, sel_hidden = [], []
    for r in rows:
        sid = r.get("id")
        if not sid:
            continue
        if sid in hidden_ids:
            if len(sel_hidden) < 50:
                sel_hidden.append(r)
        elif len(sel_visible) < limit:
            sel_visible.append(r)
        if len(sel_visible) >= limit and len(sel_hidden) >= 50:
            break

    locks = CODEX_HOME / "thread-writer-locks"
    live = {p.stem for p in locks.glob("*.lock")} if locks.is_dir() else set()
    home = os.path.expanduser("~")
    visible = [s for r in sel_visible if (s := _row_session(r, live, home))]
    hidden = [s for r in sel_hidden if (s := _row_session(r, live, home))]
    hidden.sort(key=lambda s: s["last_active"] or 0, reverse=True)
    return {"visible": visible, "hidden": hidden}


def probe(scan: ProcScan) -> list[AgentStatus]:
    procs = _interactive_codex(scan)
    title, title_ts = _latest_title()
    write_ts = _recent_write_mtime()

    if not procs:
        return [AgentStatus(
            id="codex", name="Codex CLI", kind="cli", state="offline",
            activity="未运行",
            title=title,
            last_active=write_ts or title_ts,
            enter={"type": "iterm", "needle": "codex"},
            has_sessions=True,
            stats=collect_stats(),
        )]

    cpu, mem = scan.usage(procs)
    active = write_ts is not None and (time.time() - write_ts) < RECENT_WINDOW
    last = write_ts or title_ts
    return [AgentStatus(
        id="codex", name="Codex CLI", kind="cli",
        state="busy" if active else "idle",
        activity="正在执行任务" if active else "空闲等待输入",
        title=title,
        last_active=last,
        pid=procs[0].info["pid"],
        cpu_percent=cpu or None,
        mem_mb=mem or None,
        enter={"type": "iterm", "needle": "codex"},
        has_sessions=True,
        stats=collect_stats(),
        detail={"app_server_backends": len(scan.with_cmdline("codex", "app-server"))},
    )]
