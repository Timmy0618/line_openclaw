"""LINE webhook bridge for OpenClaw.

Receives LINE Messaging API webhooks, validates the HMAC-SHA256 signature,
then runs an OpenClaw agent turn via `docker exec` and replies to LINE.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any, Dict, List

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENCLAW_CONTAINER = os.environ.get("OPENCLAW_CONTAINER", "line_openclaw-openclaw-1")
OPENCLAW_TIMEOUT_SECONDS = float(os.environ.get("OPENCLAW_TIMEOUT_SECONDS", "25"))
GROUP_BUFFER_SIZE = int(os.environ.get("GROUP_BUFFER_SIZE", "20"))
# 允許互動的 LINE userId 白名單；空集合 = 不限制（僅對 1-on-1 DM 有效）
ALLOWED_USER_IDS: set[str] = {
    uid.strip()
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}
# 允許 bot 回應的群組／聊天室 ID 白名單；空集合 = 不限制
ALLOWED_GROUP_IDS: set[str] = {
    gid.strip()
    for gid in os.environ.get("ALLOWED_GROUP_IDS", "").split(",")
    if gid.strip()
}

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
UPSTREAM_ERROR_MESSAGE = "抱歉，服務暫時無法回應"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("line_bridge")

# ---------------------------------------------------------------------------
# Per-session locks and group message buffers
# ---------------------------------------------------------------------------

_session_locks: dict[str, asyncio.Lock] = {}
_session_locks_meta_lock = asyncio.Lock()

# group message buffer: session_id -> list of {user_id, text}
# in-memory only; cleared on restart
_group_buffers: dict[str, list[dict]] = {}


async def _get_or_create_session_lock(session_id: str) -> asyncio.Lock:
    async with _session_locks_meta_lock:
        if session_id not in _session_locks:
            _session_locks[session_id] = asyncio.Lock()
            logger.info("New session lock created for %s", session_id)
        return _session_locks[session_id]


def _buffer_append(session_id: str, user_id: str, text: str) -> None:
    buf = _group_buffers.setdefault(session_id, [])
    buf.append({"user_id": user_id, "text": text})
    if len(buf) > GROUP_BUFFER_SIZE:
        buf.pop(0)


def _format_buffer_context(session_id: str) -> str:
    buf = _group_buffers.get(session_id, [])
    if not buf:
        return ""
    lines = "\n".join(f"{m['user_id']}: {m['text']}" for m in buf)
    return f"（以下是本次對話的近期群組訊息，供你參考）\n{lines}\n（以上結束）\n\n"


# ---------------------------------------------------------------------------
# Lifespan: shared httpx client
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient(timeout=30.0)
    if not LINE_CHANNEL_SECRET:
        logger.error("LINE_CHANNEL_SECRET is empty; all webhooks will fail signature check")
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.error("LINE_CHANNEL_ACCESS_TOKEN is empty; replies will fail")
    logger.info(
        "line-bridge started (container=%s timeout=%.1fs buffer_size=%d)",
        OPENCLAW_CONTAINER,
        OPENCLAW_TIMEOUT_SECONDS,
        GROUP_BUFFER_SIZE,
    )
    yield
    if _client is not None:
        await _client.aclose()
        _client = None


app = FastAPI(title="LINE Bridge", version="0.1.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Admin: list active sessions
# ---------------------------------------------------------------------------


@app.get("/admin/sessions")
async def admin_sessions() -> Dict[str, Any]:
    return {
        "sessions": list(_session_locks.keys()),
        "count": len(_session_locks),
    }


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _verify_signature(body: bytes, header_value: str | None) -> bool:
    if not header_value or not LINE_CHANNEL_SECRET:
        return False
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, header_value)


# ---------------------------------------------------------------------------
# OpenClaw agent call via docker exec
# ---------------------------------------------------------------------------


async def _ask_openclaw(text: str, session_id: str) -> str:
    lock = await _get_or_create_session_lock(session_id)
    if lock.locked():
        logger.info("Session %s busy, queuing request: %r", session_id, text[:60])
    async with lock:
        return await _ask_openclaw_inner(text, session_id)


async def _ask_openclaw_inner(text: str, session_id: str) -> str:
    cmd = [
        "docker", "exec", OPENCLAW_CONTAINER,
        "node", "openclaw.mjs", "agent",
        "--session-id", session_id,
        "--message", text,
        "--json",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=OPENCLAW_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.error("OpenClaw agent timed out after %.1fs", OPENCLAW_TIMEOUT_SECONDS)
        return UPSTREAM_ERROR_MESSAGE
    except Exception as exc:
        logger.exception("docker exec failed: %s", exc)
        return UPSTREAM_ERROR_MESSAGE

    if proc.returncode != 0:
        logger.error("OpenClaw agent exit %d: %s", proc.returncode, stderr.decode()[:300])
        return UPSTREAM_ERROR_MESSAGE

    try:
        data = json.loads(stdout)
        payloads = data["result"]["payloads"]
        texts = [p["text"] for p in payloads if p.get("text")]
        return "\n".join(texts) or UPSTREAM_ERROR_MESSAGE
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        logger.exception("Unexpected OpenClaw response: %s\nraw: %s", exc, stdout[:300])
        return UPSTREAM_ERROR_MESSAGE


# ---------------------------------------------------------------------------
# LINE reply
# ---------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]+?)\s*```", re.DOTALL)


def _parse_line_messages(text: str) -> list[dict] | None:
    """Extract LINE message objects from an OpenClaw text response.

    Looks for a JSON code block; falls back to parsing the whole text.
    Returns a list of LINE message dicts (max 5), or None for plain prose.
    """
    match = _CODE_BLOCK_RE.search(text)
    candidate = match.group(1) if match else text.strip()

    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None

    if isinstance(data, list):
        if data and all(isinstance(m, dict) and "type" in m for m in data):
            return data[:5]
    elif isinstance(data, dict):
        t = data.get("type", "")
        if t == "flex":
            return [data]
        if t in ("bubble", "carousel"):
            return [{"type": "flex", "altText": "訊息", "contents": data}]

    return None


async def _reply_to_line(reply_token: str, text: str) -> None:
    assert _client is not None
    messages = _parse_line_messages(text)
    if messages is None:
        messages = [{"type": "text", "text": text[:5000]}]

    try:
        response = await _client.post(
            LINE_REPLY_URL,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"replyToken": reply_token, "messages": messages},
        )
        if response.status_code >= 400:
            logger.error(
                "LINE reply failed status=%s body=%s",
                response.status_code,
                response.text[:200],
            )
    except httpx.HTTPError as exc:
        logger.exception("LINE reply transport error: %s", exc)


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------


async def _handle_event(event: Dict[str, Any]) -> None:
    logger.info("Event received | type=%s", event.get("type"))
    if event.get("type") != "message":
        return
    message = event.get("message") or {}
    reply_token = event.get("replyToken")
    if message.get("type") != "text":
        if reply_token and message.get("type") == "image":
            await _reply_to_line(reply_token, "抱歉，目前不支援圖片訊息，請傳送文字。")
        return
    text = message.get("text") or ""
    if not reply_token or not text:
        return

    source = event.get("source") or {}
    source_type = source.get("type", "user")
    user_id = source.get("userId", "")

    if source_type == "user":
        session_id = f"line-user-{user_id}"
        group_context = ""
    else:
        raw_id = source.get("groupId") or source.get("roomId") or ""
        session_id = f"line-{source_type}-{raw_id}"
        group_context = f" group={raw_id}"

    logger.info("Incoming message | type=%s%s user=%s text=%r", source_type, group_context, user_id, text[:80])

    if source_type == "user":
        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            logger.info("Blocked DM from non-allowlisted user %s", user_id)
            return
    else:
        group_id = source.get("groupId") or source.get("roomId") or ""
        if ALLOWED_GROUP_IDS and group_id not in ALLOWED_GROUP_IDS:
            logger.info("Blocked group message from non-allowlisted group %s", group_id)
            return
        mentionees = (message.get("mention") or {}).get("mentionees") or []
        bot_mention = next((m for m in mentionees if m.get("isSelf")), None)
        if not bot_mention:
            # Passive monitoring: buffer the message, no OpenClaw call
            _buffer_append(session_id, user_id, text)
            logger.info("Buffered group message | session=%s user=%s", session_id, user_id)
            return
        # Strip @mention text, prepend buffer context
        idx = bot_mention.get("index", 0)
        length = bot_mention.get("length", 0)
        text = (text[:idx] + text[idx + length:]).strip()
        if not text:
            logger.info("Ignored group message (only @mention, no body) from user=%s", user_id)
            return
        context = _format_buffer_context(session_id)
        text = context + text

    logger.info("Forwarding to OpenClaw | session=%s user=%s text=%r", session_id, user_id, text[:80])
    reply = await _ask_openclaw(text, session_id)
    logger.info("Reply to user=%s | reply=%r", user_id, reply[:80])
    await _reply_to_line(reply_token, reply)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


@app.post("/line/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str | None = Header(default=None, alias="X-Line-Signature"),
) -> JSONResponse:
    body = await request.body()
    if not _verify_signature(body, x_line_signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid signature"
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json"
        ) from exc

    events: List[Dict[str, Any]] = payload.get("events") or []
    await asyncio.gather(*(_handle_event(ev) for ev in events), return_exceptions=True)

    return JSONResponse({"ok": True})
