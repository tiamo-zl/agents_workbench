"""看板配置持久化：config.json（工作区根目录）。

结构：
{
  "hidden": ["cherry-studio", ...],          # 隐藏（不下发）的 agent id
  "names": {"claude": "Claude Code CLI"},    # 内置 agent 的显示名覆盖（删除即恢复默认）
  "hidden_sessions": {                        # 会话弹窗里被隐藏的会话，按 agent 分开
    "claude": ["<session-id>", ...],
    "codex": ["<session-id>", ...]
  },
  "session_names": {                          # 会话的自定义显示名（覆盖原标题，仅本地）
    "claude": {"<session-id>": "自定义名"}
  },
  "custom": [                                 # 用户手动添加的自定义 agent
    {"id": "custom-warp", "name": "Warp", "type": "app", "match": "Warp.app"},
    {"id": "custom-aider", "name": "aider", "type": "cli", "match": "aider"},
    {"id": "custom-lmstudio", "name": "LM Studio", "type": "web", "host": "127.0.0.1", "port": 1234}
  ]
}
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"
_lock = threading.Lock()


def load() -> dict:
    """读取配置；文件缺失/损坏时返回默认空配置（不抛错）。"""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            hs = data.get("hidden_sessions") or {}
            sn = data.get("session_names") or {}
            return {
                "hidden": [h for h in data.get("hidden", []) if isinstance(h, str)],
                "names": {str(k): str(v).strip()
                          for k, v in (data.get("names") or {}).items()
                          if isinstance(k, str) and isinstance(v, str) and v.strip()},
                "hidden_sessions": {str(k): [s for s in v if isinstance(s, str)]
                                    for k, v in hs.items()
                                    if isinstance(k, str) and isinstance(v, list)},
                "session_names": {str(k): {str(s): str(n).strip()
                                           for s, n in v.items()
                                           if isinstance(s, str) and isinstance(n, str) and n.strip()}
                                  for k, v in sn.items()
                                  if isinstance(k, str) and isinstance(v, dict)},
                "custom": [c for c in data.get("custom", [])
                           if isinstance(c, dict) and c.get("id") and c.get("name")],
            }
    except (OSError, ValueError):
        pass
    return {"hidden": [], "names": {}, "hidden_sessions": {}, "session_names": {}, "custom": []}


def save(data: dict) -> None:
    """原子写回（tmp + rename），多线程下加锁。"""
    with _lock:
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)


def slugify(name: str) -> str:
    """把显示名压成 URL 安全的 id 片段。"""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s or f"agent-{uuid.uuid4().hex[:4]}"
