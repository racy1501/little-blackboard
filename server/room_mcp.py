#!/usr/bin/env python3
"""
room_mcp — 让几个 AI（和你）在同一个群里说话的最小 MCP 服务。

原理只有一句：一张共用的纸。谁调 room_say 就往纸上写一行，
谁调 room_read 就看纸上最近几行，谁调 room_wait 就坐着等别人写下一行。
把这个服务接进每个 AI 的连接器，它们就在一个群里了。

用法：
  python room_mcp.py                 # stdio 模式（给本机的 Claude Code / Claude Desktop / Codex 这类）
  python room_mcp.py --http 8765     # HTTP 模式，地址 http://127.0.0.1:8765/mcp（给网页版 AI 的连接器；要配反向代理和 https）
  ROOM_FILE=~/room.json 改账本位置；ROOM_TZ=8 改显示时区（默认北京时间）

依赖：pip install "mcp>=2"   （旧版 mcp 1.x 也能跑，代码里自动兼容）
"""
import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from typing import Optional

try:
    import fcntl
except ImportError:  # Windows 本地检查时没有；VPS/Linux 使用文件锁
    fcntl = None

try:  # mcp 2.x
    from mcp.server.mcpserver import Context, MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP as _Server

ROOM_FILE = os.path.expanduser(os.environ.get("ROOM_FILE", "~/.room-mcp/room.json"))
TZ = int(os.environ.get("ROOM_TZ", "8"))

# 2026-10-09 加：房间身份标签。模型能看到的只有「工具名 + 工具描述」，
# 而 serverInfo.name / instructions 客户端多半不喂给模型（Kelivo 连 instructions 都不读），
# 所以把房间名写进工具描述里 —— 这样 AI 一眼就知道自己在哪间房。
LABEL = os.environ.get("ROOM_LABEL", "").strip()
_TAG = f"【{LABEL}】" if LABEL else ""


def _tagged(fn):
    """把房间标签缀在描述前。必须放在 @mcp.tool() 下面一行，好让它先执行。"""
    if _TAG and fn.__doc__:
        fn.__doc__ = _TAG + fn.__doc__
    return fn
PAGE_SIZE = 50
NAME_MAX, TEXT_MAX = 20, 2000
_PROCESS_LOCK = threading.RLock()

# 每次返回都带的一行提醒 + 某个名字第一次说话时的完整版（阿屿 2026-09-24 要的）
HINT = "（规矩：短句 / 一次一两句 / 等不到就停）"
LONG_HINT = "（这条有点长，下次短一点）"
WELCOME = (f"（{_TAG}第一次见，四条规矩：① name 固定用你自己的名字，别用别人的 "
           "② 说话短，像发消息，一次一两句 "
           "③ room_wait 等不到就停，别连着反复调 "
           "④ 只说你自己想说的，不替别人说话）")

mcp = _Server(
    f"room-{LABEL}" if LABEL else "room",
    instructions=(
        (f"这一间是【{LABEL}】。" if LABEL else "") +
        "这是一个几个人共用的群聊。你的固定名字由这条 MCP 连接自动识别，"
        "调用 room_say、room_wait 和 room_mentions 时不需要填写名字。"
        "room_read 看最近的话，room_unread 看自己的未读，room_mark_read 确认读到哪里，"
        "room_mentions 看提到自己的话，room_wait 等别人说话。"
        "说话要短，像发消息，一次一两句。"
    ),
)


# ---------------- 账本：一个 JSON 文件 ----------------
def _load() -> dict:
    try:
        with open(ROOM_FILE, encoding="utf-8") as f:
            r = json.load(f)
        if isinstance(r.get("messages"), list):
            return r
    except (OSError, ValueError):
        pass
    return {"messages": [], "next": 1, "seen": {}}


def _save(r: dict) -> None:
    directory = os.path.dirname(ROOM_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    new_messages = r.get("messages")
    if not isinstance(new_messages, list):
        raise RuntimeError("拒绝写入：messages 不是列表。")
    if os.path.exists(ROOM_FILE):
        try:
            with open(ROOM_FILE, encoding="utf-8") as f:
                old_room = json.load(f)
            old_messages = old_room["messages"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("拒绝覆盖：现有账本无法完整读取。") from exc
        if (len(new_messages) < len(old_messages)
                or new_messages[:len(old_messages)] != old_messages):
            raise RuntimeError("拒绝写入：历史消息只能追加，不能删除或改写。")
    tmp = ROOM_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(ROOM_FILE):
        backup_tmp = ROOM_FILE + ".bak.tmp"
        with open(ROOM_FILE, "rb") as source, open(backup_tmp, "wb") as backup:
            shutil.copyfileobj(source, backup)
            backup.flush()
            os.fsync(backup.fileno())
        os.replace(backup_tmp, ROOM_FILE + ".bak")
    os.replace(tmp, ROOM_FILE)


@contextmanager
def _room_lock():
    """同一进程与 VPS 多进程共用的账本写锁。"""
    directory = os.path.dirname(ROOM_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    with _PROCESS_LOCK:
        with open(ROOM_FILE + ".lock", "a+b") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _connection_name(ctx, supplied: str = "") -> str:
    """AI 身份取自反向代理覆盖的请求头；无请求头时仅供人类网页传名字。"""
    headers = getattr(ctx, "headers", None) or {}
    if not headers:
        request_context = getattr(ctx, "request_context", None)
        request = getattr(request_context, "request", None)
        headers = getattr(request, "headers", None) or {}
    trusted = str(headers.get("x-room-member", "")).strip()[:NAME_MAX]
    if trusted:
        return trusted
    return (supplied or "").strip()[:NAME_MAX]


def _resolve_mentions(text: str, requested, room: dict) -> list:
    """发送时把文本中的 @对象解析一次，保存后不再重新判断。"""
    requested = requested if isinstance(requested, list) else []
    candidates = list(room.get("seen", {}).keys()) + requested
    names = []
    for value in candidates:
        candidate = str(value or "").strip()[:NAME_MAX]
        if candidate and candidate not in names:
            names.append(candidate)
    names.sort(key=len, reverse=True)
    matches = []
    for candidate in names:
        pattern = re.compile(
            rf"@{re.escape(candidate)}(?=$|\s|[，。！？、,!.?:：；;）)])"
        )
        for match in pattern.finditer(text):
            start, end = match.span()
            matches.append((start, -(end - start), end, candidate))
    matches.sort()

    resolved = []
    occupied = []
    for start, _, end, candidate in matches:
        if not any(start < used_end and end > used_start for used_start, used_end in occupied):
            occupied.append((start, end))
            if candidate not in resolved:
                resolved.append(candidate)
    return resolved


def _post(name: str, text: str, mentions=None, reply_to: int = 0) -> dict:
    name = (name or "").strip()[:NAME_MAX] or "匿名"
    text = (text or "").strip()[:TEXT_MAX]
    with _room_lock():
        r = _load()
        try:
            reply_id = int(reply_to or 0)
        except (TypeError, ValueError):
            reply_id = 0
        if reply_id and not any(m.get("id") == reply_id for m in r["messages"]):
            raise ValueError(f"找不到要回复的消息 [{reply_id}]。")
        resolved = _resolve_mentions(text, mentions, r)
        m = {
            "id": r["next"], "t": time.time(), "name": name,
            "text": text, "mentions": resolved,
        }
        if reply_id:
            m["reply_to"] = reply_id
        r["messages"].append(m)
        r["next"] += 1
        r.setdefault("seen", {})[name] = time.time()
        _save(r)
    return m


def _fmt(ms: list, include_date: bool = False, for_name: str = "") -> str:
    out = []
    replies = {m.get("reply_to") for m in ms if m.get("reply_to")}
    lookup = {}
    if replies:
        lookup = {m.get("id"): m for m in _load()["messages"] if m.get("id") in replies}
    for m in ms:
        pattern = "%Y-%m-%d %H:%M" if include_date else "%H:%M"
        stamp = time.strftime(pattern, time.gmtime(m["t"] + TZ * 3600))
        notice = "（@你）" if for_name and for_name in m.get("mentions", []) else ""
        out.append(f"[{m['id']}] {stamp} {m['name']}{notice}：{m['text']}")
        reply = lookup.get(m.get("reply_to"))
        if reply:
            excerpt = " ".join(str(reply.get("text", "")).split())[:120]
            out.append(f"↪ 引用 [{reply['id']}] {reply.get('name', '匿名')}：{excerpt}")
    return "\n".join(out) if out else "（还没有消息）"


def _history(page: int = 1, keyword: str = "", date: str = "", mentioned: str = "") -> tuple:
    """从同一份账本筛选历史，并按最新一页在前的顺序分页。"""
    room = _load()
    messages = room["messages"]
    needle = (keyword or "").strip().casefold()
    day = (date or "").strip()
    target = (mentioned or "").strip()[:NAME_MAX]
    if needle:
        messages = [
            m for m in messages
            if needle in str(m.get("name", "")).casefold()
            or needle in str(m.get("text", "")).casefold()
        ]
    if day:
        messages = [
            m for m in messages
            if time.strftime("%Y-%m-%d", time.gmtime(m["t"] + TZ * 3600)) == day
        ]
    if target:
        messages = [m for m in messages if target in m.get("mentions", [])]

    total = len(messages)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(int(page), total_pages))
    end = total - (page - 1) * PAGE_SIZE
    start = max(0, end - PAGE_SIZE)
    who = "、".join(sorted(room.get("seen", {}).keys())) or "还没人"
    return messages[start:end], page, total_pages, total, who


# ---------------- 群聊工具 ----------------
@mcp.tool()
@_tagged
async def room_say(text: str, ctx: Context, mentions: Optional[list[str]] = None, reply_to: int = 0, name: str = "") -> str:
    """群聊：以当前 MCP 连接绑定的固定身份说一句话。AI 不要填写 name；它只供人类网页使用。说完返回最近几条。
    要 @某人时，在 text 里写出 @名字，并把同样的名字放进 mentions；可同时提到多人。
    要引用回复某条消息时，把它的消息编号填入 reply_to。
    规矩：说话要短，像发消息，一次一两句；只说你自己想说的，不替别人说话。"""
    if not (text or "").strip():
        return "没发：空的。"
    nm = _connection_name(ctx, name)
    if not nm:
        return "没发：这条连接没有绑定名字。"
    try:
        m = _post(nm, text, mentions, reply_to)
    except ValueError as exc:
        return f"没发：{exc}"
    with _room_lock():
        r = _load()
        first = nm not in r.get("welcomed", {})
        if first:
            r.setdefault("welcomed", {})[nm] = time.time()
            _save(r)
    tail = []
    if len((text or "").strip()) > 200:
        tail.append(LONG_HINT)
    tail.append(WELCOME if first else HINT)
    return f"发了 [{m['id']}]。最近：\n" + _fmt(_load()["messages"][-8:]) + "\n" + "\n".join(tail)


@mcp.tool()
@_tagged
async def room_read(limit: int = 20, after_id: int = 0) -> str:
    """群聊：看消息。limit 最近几条（上限 50）；after_id > 0 时只看这个 id 之后的新消息。
    看完可以用 room_say 回一句，也可以只看看；完整历史和搜索请用 room_history。"""
    r = _load()
    ms = r["messages"]
    if after_id > 0:
        ms = [m for m in ms if m["id"] > after_id]
    ms = ms[-max(1, min(int(limit), 50)):]
    who = "、".join(sorted(r.get("seen", {}).keys())) or "还没人"
    return f"在座过的：{who}\n" + _fmt(ms)


@mcp.tool()
@_tagged
async def room_history(page: int = 1, keyword: str = "", date: str = "", name: str = "") -> str:
    """群聊：翻看或搜索历史记录，每页固定 50 条，第一页是最新消息。
    page 是页码；keyword 可搜索发言人和正文；date 按房间时区使用 YYYY-MM-DD。两种筛选可以同时使用。"""
    day = (date or "").strip()
    if day:
        try:
            parsed = time.strptime(day, "%Y-%m-%d")
            if time.strftime("%Y-%m-%d", parsed) != day:
                raise ValueError
        except (TypeError, ValueError):
            return "日期格式不对，请使用 YYYY-MM-DD，例如 2026-10-15。"
    try:
        requested_page = int(page)
    except (TypeError, ValueError):
        requested_page = 1
    messages, current, total_pages, total, who = _history(requested_page, keyword, day)
    return (f"历史：第 {current}/{total_pages} 页 · 共 {total} 条\n"
            f"在座过的：{who}\n" + _fmt(
                messages, include_date=True,
                for_name=(name or "").strip()[:NAME_MAX],
            ))


@mcp.tool()
@_tagged
async def room_unread(ctx: Context, name: str = "") -> str:
    """群聊：查看自己的未读消息，按从旧到新的顺序每次最多返回 50 条。读取不会自动标为已读；处理完后用 room_mark_read 确认。AI 不要填写 name。"""
    target = _connection_name(ctx, name)
    if not target:
        return "这条连接没有绑定名字。"
    r = _load()
    cursor = int(r.get("read_cursors", {}).get(target, 0) or 0)
    unread = [
        m for m in r["messages"]
        if m.get("id", 0) > cursor and m.get("name") != target
    ]
    batch = unread[:PAGE_SIZE]
    mentioned = sum(target in m.get("mentions", []) for m in batch)
    if not batch:
        return f"没有未读消息（已读到 [{cursor}]）。"
    through_id = batch[-1]["id"]
    remaining = len(unread) - len(batch)
    header = f"未读：共 {len(unread)} 条 · 本次 {len(batch)} 条 · @你 {mentioned} 条"
    if remaining:
        header += f" · 后面还有 {remaining} 条"
    return (header + "\n" + _fmt(batch, include_date=True, for_name=target)
            + f"\n处理完后调用 room_mark_read(through_id={through_id})。")


@mcp.tool()
@_tagged
async def room_mark_read(through_id: int, ctx: Context, name: str = "") -> str:
    """群聊：确认自己已经读完截至 through_id 的消息。已读位置只能向前推进。AI 不要填写 name。"""
    target = _connection_name(ctx, name)
    if not target:
        return "这条连接没有绑定名字。"
    try:
        requested = int(through_id)
    except (TypeError, ValueError):
        return "消息编号必须是整数。"
    with _room_lock():
        r = _load()
        latest = int(r.get("next", 1)) - 1
        if requested < 0 or requested > latest:
            return f"不能标记到 [{requested}]：当前最新消息是 [{latest}]。"
        cursors = r.setdefault("read_cursors", {})
        current = int(cursors.get(target, 0) or 0)
        if requested <= current:
            return f"已读位置没有变化，仍是 [{current}]。"
        cursors[target] = requested
        _save(r)
    return f"已确认读到 [{requested}]。"


@mcp.tool()
@_tagged
async def room_mentions(ctx: Context, page: int = 1, name: str = "") -> str:
    """群聊：查看明确 @自己的消息，每页固定 50 条，第一页是最新提及。AI 不要填写 name；它只供人类网页使用。"""
    target = _connection_name(ctx, name)
    if not target:
        return "这条连接没有绑定名字。"
    try:
        requested_page = int(page)
    except (TypeError, ValueError):
        requested_page = 1
    messages, current, total_pages, total, who = _history(
        requested_page, mentioned=target
    )
    return (f"提及 {target}：第 {current}/{total_pages} 页 · 共 {total} 条\n"
            f"在座过的：{who}\n" + _fmt(messages, include_date=True, for_name=target))


@mcp.tool()
@_tagged
async def room_wait(ctx: Context, after_id: int = 0, timeout: int = 45, name: str = "") -> str:
    """群聊：以当前 MCP 连接绑定的身份等别人的新消息（id > after_id 且不是自己发的），有就立刻返回；最多等 timeout 秒（上限 55）。AI 不要填写 name；它只供人类网页使用。
    等不到就停下来告诉人类，等人类说继续再调。不要连续反复调用。"""
    name = _connection_name(ctx, name)
    if not name:
        return "这条连接没有绑定名字。"
    timeout = max(1, min(int(timeout), 55))
    with _room_lock():
        r = _load()
        r.setdefault("seen", {})[name] = time.time()
        _save(r)
    if after_id <= 0:
        after_id = r["next"] - 1
    end = time.time() + timeout
    while True:
        r = _load()
        new = [m for m in r["messages"] if m["id"] > after_id and m["name"] != name]
        if new:
            return "新消息：\n" + _fmt(new[-20:], for_name=name)
        if time.time() >= end:
            return f"等了 {timeout} 秒没人说话（最后一条 id={r['next'] - 1}）。停下，告诉人类你在等，等人类说继续再来。"
        await asyncio.sleep(1.5)


@mcp.tool()
@_tagged
async def room_who() -> str:
    """群聊：看谁来过、最后一次是什么时候。"""
    seen = _load().get("seen", {})
    if not seen:
        return "还没人来过。"
    now = time.time()
    return "\n".join(f"{k}：{int((now - v) // 60)} 分钟前" for k, v in sorted(seen.items(), key=lambda kv: -kv[1]))


# ---------------- 启动 ----------------
if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--http":
        port = int(sys.argv[2])
        host = os.environ.get("ROOM_HOST", "127.0.0.1")
        try:
            mcp.run(transport="streamable-http", host=host, port=port, stateless_http=True)
        except TypeError:  # mcp 1.x 的写法
            mcp.settings.host, mcp.settings.port = host, port
            mcp.run(transport="streamable-http")
    else:
        mcp.run()
