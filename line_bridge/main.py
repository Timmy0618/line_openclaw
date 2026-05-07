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
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
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
SESSION_ROTATION_HOURS = float(os.environ.get("SESSION_ROTATION_HOURS", "3"))
SESSION_CHECK_INTERVAL_SECONDS = 300  # 每 5 分鐘檢查一次 session 年齡
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
SESSIONS_FILE = Path("/readonly-openclaw/agents/main/sessions/sessions.json")
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
# Session discovery
# ---------------------------------------------------------------------------

_session_id: str = ""
_session_started_at: float = 0.0


def _parse_iso_to_timestamp(raw: str) -> float:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return time.time()


def _discover_session_info() -> tuple[str, float]:
    """Return (session_id, started_at_unix_ts). started_at defaults to now on parse failure."""
    try:
        data = json.loads(SESSIONS_FILE.read_text())
        for session in data.values():
            if isinstance(session, dict) and "sessionId" in session:
                session_id = session["sessionId"]
                started_ts = _parse_iso_to_timestamp(session.get("sessionStartedAt", ""))
                return session_id, started_ts
    except Exception as exc:
        logger.error("Failed to read sessions file: %s", exc)
    return "", time.time()


# ---------------------------------------------------------------------------
# Lifespan: shared httpx client + session rotation background task
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None
_openclaw_lock: asyncio.Lock | None = None
_rotation_task: asyncio.Task | None = None


async def _rotate_session() -> bool:
    """Create a new OpenClaw session. Caller must hold _openclaw_lock."""
    global _session_id, _session_started_at
    new_id = str(uuid.uuid4())
    cmd = [
        "docker", "exec", OPENCLAW_CONTAINER,
        "node", "openclaw.mjs", "agent",
        "--session-id", new_id,
        "--message", ".",
        "--json",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=OPENCLAW_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            logger.error(
                "Session rotation failed (exit %d): %s",
                proc.returncode,
                stderr.decode()[:200],
            )
            return False
    except asyncio.TimeoutError:
        logger.error("Session rotation timed out after %.1fs", OPENCLAW_TIMEOUT_SECONDS)
        return False
    except Exception as exc:
        logger.exception("Session rotation error: %s", exc)
        return False

    old_id = _session_id
    _session_id = new_id
    _session_started_at = time.time()
    logger.info("Session rotated: %s → %s", old_id, new_id)
    return True


async def _session_rotation_loop() -> None:
    while True:
        await asyncio.sleep(SESSION_CHECK_INTERVAL_SECONDS)
        age = time.time() - _session_started_at
        if age < SESSION_ROTATION_HOURS * 3600:
            continue
        logger.info(
            "Session age %.1fh ≥ %.1fh, rotating…",
            age / 3600,
            SESSION_ROTATION_HOURS,
        )
        assert _openclaw_lock is not None
        async with _openclaw_lock:
            await _rotate_session()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _session_id, _session_started_at, _openclaw_lock, _rotation_task
    _client = httpx.AsyncClient(timeout=30.0)
    _openclaw_lock = asyncio.Lock()
    if not LINE_CHANNEL_SECRET:
        logger.error("LINE_CHANNEL_SECRET is empty; all webhooks will fail signature check")
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.error("LINE_CHANNEL_ACCESS_TOKEN is empty; replies will fail")
    _session_id, _session_started_at = _discover_session_info()
    if not _session_id:
        logger.error("Could not discover OpenClaw session ID from %s", SESSIONS_FILE)
    logger.info(
        "line-bridge started (container=%s session=%s timeout=%.1fs rotation=%.1fh)",
        OPENCLAW_CONTAINER,
        _session_id,
        OPENCLAW_TIMEOUT_SECONDS,
        SESSION_ROTATION_HOURS,
    )
    _rotation_task = asyncio.create_task(_session_rotation_loop())
    yield
    _rotation_task.cancel()
    with suppress(asyncio.CancelledError):
        await _rotation_task
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
# Admin: manual session rotation
# ---------------------------------------------------------------------------


@app.post("/admin/rotate-session")
async def admin_rotate_session() -> Dict[str, Any]:
    assert _openclaw_lock is not None
    async with _openclaw_lock:
        ok = await _rotate_session()
    return {"rotated": ok, "session_id": _session_id}


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


async def _ask_openclaw(text: str) -> str:
    if not _session_id:
        logger.error("No session ID available")
        return UPSTREAM_ERROR_MESSAGE

    assert _openclaw_lock is not None
    if _openclaw_lock.locked():
        logger.info("OpenClaw busy, queuing request: %r", text[:60])
    async with _openclaw_lock:
        return await _ask_openclaw_inner(text)


async def _ask_openclaw_inner(text: str) -> str:
    cmd = [
        "docker", "exec", OPENCLAW_CONTAINER,
        "node", "openclaw.mjs", "agent",
        "--session-id", _session_id,
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


async def _reply_to_line(reply_token: str, text: str) -> None:
    assert _client is not None
    try:
        response = await _client.post(
            LINE_REPLY_URL,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": text[:5000]}],
            },
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
    if message.get("type") != "text":
        return
    text = message.get("text") or ""
    reply_token = event.get("replyToken")
    if not reply_token or not text:
        return

    source = event.get("source") or {}
    source_type = source.get("type", "user")
    user_id = source.get("userId", "")

    group_context = ""
    if source_type != "user":
        raw_group_id = source.get("groupId") or source.get("roomId") or ""
        group_context = f" group={raw_group_id}"
    logger.info("Incoming message | type=%s%s user=%s text=%r", source_type, group_context, user_id, text[:80])

    if source_type == "user":
        # 1-on-1 DM：白名單不為空時才限制
        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            logger.info("Blocked DM from non-allowlisted user %s", user_id)
            return
    else:
        # 群組／聊天室：先做群組白名單檢查，再看是否被 @mention
        group_id = source.get("groupId") or source.get("roomId") or ""
        if ALLOWED_GROUP_IDS and group_id not in ALLOWED_GROUP_IDS:
            logger.info("Blocked group message from non-allowlisted group %s", group_id)
            return
        mentionees = (message.get("mention") or {}).get("mentionees") or []
        bot_mention = next((m for m in mentionees if m.get("isSelf")), None)
        if not bot_mention:
            logger.info("Ignored group message (no @mention) from user=%s", user_id)
            return
        # 去除 @mention 文字，只傳純訊息給 OpenClaw
        idx = bot_mention.get("index", 0)
        length = bot_mention.get("length", 0)
        text = (text[:idx] + text[idx + length:]).strip()
        if not text:
            logger.info("Ignored group message (only @mention, no body) from user=%s", user_id)
            return

    logger.info("Forwarding to OpenClaw | user=%s text=%r", user_id, text[:80])
    reply = await _ask_openclaw(text)
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
