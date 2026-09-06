"""FastAPI 服务：看板页面、SSE 状态流、进入动作、任务流水线 API。

只绑 127.0.0.1，仅本机使用。卡片数据自带 enter 动作描述（见 base.py），
进入动作直接查最新一帧即可。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config as board_config, control, registry
from .agents import custom as custom_agents
from .agents import dsh as dsh_agent
from .agents.base import AgentStatus
from .tasks import TaskManager, validate_steps

SESSION_PROVIDERS = registry.SESSION_PROVIDERS

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="Agent 看板", docs_url=None, redoc_url=None)
tasks = TaskManager()


@app.middleware("http")
async def no_cache_static(request, call_next):
    """静态资源禁强缓存（保留 ETag 协商）：前端改完普通刷新即生效。"""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response


class StepIn(BaseModel):
    agent: str
    prompt: str = Field(min_length=1)


class TaskIn(BaseModel):
    goal: str = ""
    steps: list[StepIn]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/status")
async def status_sse():
    """SSE：每 2 秒推送一帧（agents + tasks）。"""
    async def event_stream():
        while True:
            frame = await asyncio.to_thread(registry.snapshot)
            frame["tasks"] = tasks.all()
            yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/open/{agent_id}")
def open_agent(agent_id: str):
    frame = registry.snapshot()
    card = next((c for c in frame["agents"] if c["id"] == agent_id), None)
    if not card:
        raise HTTPException(404, f"未知 agent: {agent_id}")
    status = AgentStatus(id=card["id"], name=card["name"], kind=card["kind"],
                         enter=card.get("enter") or {})
    try:
        return {"ok": True, "message": control.enter(status)}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ---------- 会话级跳转（claude / codex / mimo / hermes / gemini / dsh / cursor） ----------
# 列表 provider 统一定义在 registry.SESSION_PROVIDERS（卡片预览与跳转共用）

# 点击会话后的三种进入方式，按 agent 形态区分：
#   cli  → SESSION_COMMANDS     在 iTerm2 新标签 cd + 执行恢复命令（经 shell，参数须为安全字符）
#   app  → SESSION_APP_OPENERS  直接拉起本地应用（argv 传参，无 shell 注入面）
#   web  → SESSION_URL_OPENERS  用浏览器打开（dsh 无按会话深链，进入 Web UI 后在界面内选会话）
SESSION_COMMANDS = {
    "claude": lambda sid: f"claude --resume {sid}",
    "codex": lambda sid: f"codex resume {sid}",
    "mimo": lambda sid: f"mimo -s {sid}",
    "hermes": lambda sid: f"hermes --resume {sid}",
    "gemini": lambda sid: f"gemini --resume {sid}",
}
SESSION_APP_OPENERS = {
    "cursor": lambda sid: ["open", "-a", "Cursor", sid],
}
SESSION_URL_OPENERS = {
    "dsh": lambda sid: f"http://{dsh_agent.HOST}:{dsh_agent.PORT}",
}

# 会话正在等待审批时的跳转命令后缀：resume 不会恢复旧进程挂起的审批请求，
# 附加一句初始提示让 agent 恢复后立刻继续，待批准的操作重新发起、审批框重现
SESSION_CONTINUE_NUDGE = {"claude": "continue"}


def _sid_ok(agent_id: str, sid: str) -> bool:
    if agent_id in SESSION_APP_OPENERS:   # 应用直开类：sid 是已存在的绝对路径
        return os.path.isabs(sid) and os.path.isdir(sid)
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{4,64}", sid))


class SessionOpenIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=256)   # cursor 用路径，放长
    cwd: str = ""


@app.get("/api/sessions/{agent_id}")
def agent_sessions(agent_id: str, limit: int = 12):
    """可见 + 被隐藏的最近会话；按需拉取（打开弹窗时才调），不进 SSE 帧。"""
    provider = SESSION_PROVIDERS.get(agent_id)
    if not provider:
        raise HTTPException(404, f"agent {agent_id} 不支持会话列表")
    cfg = board_config.load()
    hidden_ids = frozenset(cfg.get("hidden_sessions", {}).get(agent_id, []))
    try:
        result = provider(max(1, min(limit, 30)), hidden_ids)
    except Exception as exc:
        raise HTTPException(500, f"读取会话失败: {exc}")
    overrides = cfg.get("session_names", {}).get(agent_id, {})
    _, waiting_map = registry.approval_markers(agent_id)
    for lst in (result["visible"], result["hidden"]):
        for s in lst:
            s["orig_title"] = s["title"]
            s["renamed"] = s["id"] in overrides
            if s["renamed"]:
                s["title"] = overrides[s["id"]]
            s["waiting"] = s["id"] in waiting_map
            s["pending_msg"] = waiting_map.get(s["id"], "")
    return {"agent": agent_id, "sessions": result["visible"], "hidden": result["hidden"]}


class SessionRenameIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=256)
    name: str = Field(max_length=100)


@app.post("/api/sessions/{agent_id}/rename")
def rename_session(agent_id: str, body: SessionRenameIn):
    """会话的自定义显示名（仅本地覆盖，不改 agent 自己的数据）；name 为空 = 恢复默认。"""
    if agent_id not in SESSION_PROVIDERS:
        raise HTTPException(404, f"agent {agent_id} 不支持会话列表")
    if not _sid_ok(agent_id, body.session_id):
        raise HTTPException(422, "非法会话 id")
    cfg = board_config.load()
    names = cfg.setdefault("session_names", {}).setdefault(agent_id, {})
    name = body.name.strip()
    if name:
        names[body.session_id] = name
    else:
        names.pop(body.session_id, None)
    board_config.save(cfg)
    return {"ok": True}


class SessionHideIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=256)
    hidden: bool


@app.post("/api/sessions/{agent_id}/hide")
def hide_session(agent_id: str, body: SessionHideIn):
    """隐藏 / 恢复显示一条会话（持久化到 config.json 的 hidden_sessions）。"""
    if agent_id not in SESSION_PROVIDERS:
        raise HTTPException(404, f"agent {agent_id} 不支持会话列表")
    if not _sid_ok(agent_id, body.session_id):
        raise HTTPException(422, "非法会话 id")
    cfg = board_config.load()
    hs = cfg.setdefault("hidden_sessions", {})
    ids = set(hs.get(agent_id, []))
    if body.hidden:
        ids.add(body.session_id)
    else:
        ids.discard(body.session_id)
    hs[agent_id] = sorted(ids)
    board_config.save(cfg)
    return {"ok": True}


@app.post("/api/open/{agent_id}/session")
def open_session(agent_id: str, body: SessionOpenIn):
    if agent_id not in SESSION_PROVIDERS:
        raise HTTPException(404, f"agent {agent_id} 不支持会话跳转")
    sid = body.session_id
    if not _sid_ok(agent_id, sid):
        raise HTTPException(422, "非法会话 id")
    try:
        if agent_id in SESSION_APP_OPENERS:      # 应用：直接拉起，不经 iTerm2
            subprocess.Popen(SESSION_APP_OPENERS[agent_id](sid))
            return {"ok": True, "message": f"已在 Cursor 打开 {sid}"}
        if agent_id in SESSION_URL_OPENERS:      # 浏览器类：打开其 Web UI
            url = SESSION_URL_OPENERS[agent_id](sid)
            return {"ok": True,
                    "message": control.open_browser(url) + "（dsh 界面内选择该会话）"}
        cwd = body.cwd.strip() or os.path.expanduser("~")   # cli：iTerm2 新标签 resume
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        command = SESSION_COMMANDS[agent_id](sid)
        if agent_id in SESSION_CONTINUE_NUDGE:
            _, waiting_map = registry.approval_markers(agent_id)
            if sid in waiting_map:   # 该会话在等审批：恢复后立刻继续，审批框重现
                command += f" {SESSION_CONTINUE_NUDGE[agent_id]}"
        return {"ok": True, "message": control.open_in_iterm(cwd, command)}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/tasks")
async def start_task(body: TaskIn):
    step_specs = [s.model_dump() for s in body.steps]
    err = validate_steps(step_specs)
    if err:
        raise HTTPException(422, err)
    task = tasks.start(body.goal, step_specs)
    return {"ok": True, "id": task.id}


@app.post("/api/tasks/{tid}/cancel")
def cancel_task(tid: str):
    if not tasks.cancel(tid):
        raise HTTPException(404, "任务不存在或已结束")
    return {"ok": True}


# ---------- agent 管理 ----------

class HideIn(BaseModel):
    id: str
    hidden: bool


class CustomIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    type: Literal["app", "cli", "web"]
    match: str = ""            # app/cli：进程 exe 或命令行里的特征串
    host: str = "127.0.0.1"    # web
    port: int | None = None
    url: str = ""              # web 可选：进入时打开的地址


@app.get("/api/manage")
def manage_list():
    """管理界面数据：全部 agent（内置+自定义）及启用状态 + /Applications 候选。"""
    cfg = board_config.load()
    hidden = set(cfg["hidden"])
    names = cfg.get("names", {})
    agents = []
    for meta in registry.BUILTIN_META:
        agents.append({**meta, "default_name": meta["name"],
                       "name": names.get(meta["id"], meta["name"]),
                       "source": "builtin", "hidden": meta["id"] in hidden})
    for c in cfg["custom"]:
        agents.append({"id": c["id"], "name": c["name"], "kind": c["type"],
                       "desc": c.get("match") or f"{c.get('host')}:{c.get('port')}",
                       "cfg": c, "source": "custom", "hidden": c["id"] in hidden})
    return {"agents": agents, "apps": custom_agents.suggestions()}


def _apply_hidden(agent_id: str, hide: bool) -> None:
    cfg = board_config.load()
    known = {m["id"] for m in registry.BUILTIN_META} | {c["id"] for c in cfg["custom"]}
    if agent_id not in known:
        raise HTTPException(404, f"未知 agent: {agent_id}")
    hidden = set(cfg["hidden"])
    if hide:
        hidden.add(agent_id)
    else:
        hidden.discard(agent_id)
    cfg["hidden"] = sorted(hidden)
    board_config.save(cfg)


@app.post("/api/manage/hidden")
def manage_hide(body: HideIn):
    _apply_hidden(body.id, body.hidden)
    return {"ok": True}


def _entry_from_body(body: CustomIn, agent_id: str) -> dict:
    """按类型校验请求体并生成 config 条目（保留给定 id）。"""
    if body.type in ("app", "cli") and not body.match.strip():
        raise HTTPException(422, "app / cli 类型需要填写识别串")
    if body.type == "web" and (not body.port or not (1 <= body.port <= 65535)):
        raise HTTPException(422, "web 类型需要 1-65535 的端口")
    entry = {"id": agent_id, "name": body.name.strip(), "type": body.type}
    if body.type in ("app", "cli"):
        entry["match"] = body.match.strip()
    else:
        entry["host"] = (body.host or "127.0.0.1").strip()
        entry["port"] = body.port
        if body.url.strip():
            entry["url"] = body.url.strip()
    return entry


@app.post("/api/manage/custom")
def manage_add(body: CustomIn):
    cfg = board_config.load()
    taken = ({m["id"] for m in registry.BUILTIN_META}
             | {c["id"] for c in cfg["custom"]})
    base_id = "custom-" + board_config.slugify(body.name)
    cid, n = base_id, 1
    while cid in taken:
        n += 1
        cid = f"{base_id}-{n}"
    cfg["custom"].append(_entry_from_body(body, cid))
    board_config.save(cfg)
    return {"ok": True, "id": cid}


@app.post("/api/manage/custom/{cid}")
def manage_update(cid: str, body: CustomIn):
    """编辑自定义 agent：更新名称/类型/识别串等（id 不变，流水线引用不受影响）。"""
    cfg = board_config.load()
    entry = next((c for c in cfg["custom"] if c["id"] == cid), None)
    if not entry:
        raise HTTPException(404, f"自定义 agent 不存在: {cid}")
    entry.clear()
    entry.update(_entry_from_body(body, cid))
    board_config.save(cfg)
    return {"ok": True, "id": cid}


class NameIn(BaseModel):
    id: str
    name: str = Field(max_length=40)


@app.post("/api/manage/name")
def manage_rename(body: NameIn):
    """改名：自定义 agent 改条目名；内置 agent 存显示名覆盖（空/与默认相同 = 恢复默认）。"""
    cfg = board_config.load()
    new = body.name.strip()
    custom = next((c for c in cfg["custom"] if c["id"] == body.id), None)
    if custom:
        if not new:
            raise HTTPException(422, "名称不能为空")
        custom["name"] = new
    else:
        default = next((m["name"] for m in registry.BUILTIN_META if m["id"] == body.id), None)
        if default is None:
            raise HTTPException(404, f"未知 agent: {body.id}")
        names = cfg.setdefault("names", {})
        if new and new != default:
            names[body.id] = new
        else:
            names.pop(body.id, None)
    board_config.save(cfg)
    return {"ok": True}


@app.delete("/api/manage/custom/{cid}")
def manage_delete(cid: str):
    cfg = board_config.load()
    before = len(cfg["custom"])
    cfg["custom"] = [c for c in cfg["custom"] if c["id"] != cid]
    if len(cfg["custom"]) == before:
        raise HTTPException(404, f"自定义 agent 不存在: {cid}")
    cfg["hidden"] = [h for h in cfg["hidden"] if h != cid]
    board_config.save(cfg)
    return {"ok": True}
