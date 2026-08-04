import os
import uuid
import time
import asyncio
import Auth.swiggy_auth

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path

import structlog
log = structlog.get_logger()

from configuration import load_config
load_config()

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from Agent.telegram_commands import setup_command_handlers, setup_bot_commands
import Agent.agent as agent_module
from Agent.agent import initialize_agent, send
from configuration import TOOL_LABELS, get_transcriber
from Agent.memory_and_context import run_evaluator
from Recurring_Tasks.recurring_tasks import start_recurring_tasks, set_dispatch
from Agent.session_store import init_db, load_all_sessions, load_session, save_session, delete_session
from Agent.markdown_helper import markdown_to_html
from Agent.connectors import restore_connected_connectors

SICILY_HOME = Path.home() / ".sicily"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID_FILE = SICILY_HOME / ".chat_id"

# Telegram globals
active_chat_id: int | None = None
telegram_app: Application | None = None

# OAuth future
_oauth_code_future = None

# Session Management
IDLE_MINUTES = 5
GRACEFUL_DRAIN_TIMEOUT = 10  # seconds to wait for in-flight tasks before force-cancel

@dataclass
class UserSession:
    session_id: str
    user_name: str
    chat_id: int | None = field(default=None)

    started_at: float           = field(default_factory=time.time)
    last_interaction_at: float  = field(default_factory=time.time)
    expiry_task: asyncio.Task | None = field(default=None, repr=False)

    # ── Concurrency control ───────────────────────────────────
    is_processing: bool                 = field(default=False)
    cancel_requested: bool              = field(default=False)
    active_task: asyncio.Task | None    = field(default=None, repr=False)


# key = telegram user_id (str)
_sessions: dict[str, UserSession] = {}


def format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


async def send_markdown(bot, chat_id: int, text: str, **kwargs):
    """
    Send a message with Markdown rendered as Telegram HTML.

    Falls back to plain text if the converted HTML is somehow rejected by
    Telegram (e.g. an unbalanced tag from unusual model output), so a
    formatting edge case never turns into a silently dropped message.
    """
    html_text = markdown_to_html(text)
    try:
        return await bot.send_message(chat_id=chat_id, text=html_text, parse_mode="HTML", **kwargs)
    except Exception:
        log.warning("Markdown->HTML send failed, falling back to plain text")
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)


async def edit_markdown(bot, chat_id: int, message_id: int, text: str, **kwargs):
    """Same as send_markdown but for editing an existing message."""
    html_text = markdown_to_html(text)
    try:
        return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=html_text, parse_mode="HTML", **kwargs)
    except Exception:
        log.warning("Markdown->HTML edit failed, falling back to plain text")
        return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)


async def expire_session_after_timeout(user_id: str, session_id: str, user_name: str, override_seconds: float | None = None):

    try:
        sleep_duration = override_seconds if override_seconds is not None else IDLE_MINUTES * 60
        await asyncio.sleep(sleep_duration)
    except asyncio.CancelledError:
        log.info("Session timer reset", user_id=user_id, user_name=user_name)
        return

    # ── Guard: session might have already been replaced ───────────────────
    session = _sessions.get(user_id)

    if session is None or session.session_id != session_id:
        return

    # ── Print expiry header ───────────────────────────────────────────────
    log.info(
        "Session expired",
        user_id=user_id,
        user_name=user_name,
        session_id=session_id,
        started_at=format_time(session.started_at),
        last_message_at=format_time(session.last_interaction_at),
    )

    try:
        # ── Fetch full message history from LangGraph ─────────────────────────
        config = {"configurable": {"thread_id": session_id}} # "thread_id" inside configurable is LangGraph's internal contract

        # Accessing the live graph via the module, not the stale imported None
        current_graph = agent_module.graph
        if current_graph is None:
            log.warning("Graph not yet initialized, skipping evaluator")
            return

        state = await current_graph.aget_state(config)
        messages = state.values.get("messages", [])

        if messages:

            log.info("Session message history")

            for msg in messages:
                msg_type = type(msg).__name__
                content  = getattr(msg, "content", "") or ""
                if content:
                    # truncate very long tool results so the log stays readable
                    display = content if len(content) <= 300 else content[:300] + "…"
                    log.info("Session message", message_type=msg_type, content=display)

        else:
            log.info("No messages in session")

        # ── Run evaluator on session messages ─────────────────────────────────
        await run_evaluator(session.session_id, messages)

    except Exception as e:
        log.exception("Error during session expiry", user_id=user_id)

    finally:
        _sessions.pop(user_id, None)
        await delete_session(user_id)
        log.info("Session removed from store", user_id=user_id)


# Session store logic
# get_or_create_session  →  always returns a valid (session_id, is_new) pair
# The expiry task is created/reset here by the async caller (on_telegram_message)
async def get_or_create_session(user_id: str, user_name: str) -> tuple[str, bool]:
    """
    Returns (session_id, is_new_session).

    On first call after a restart, loads the persisted session from SQLite
    so LangGraph can resume from the same thread_id (= session_id).

    Does NOT create expiry tasks — that's the async caller's job,
    because create_task must be called from an async context.

    With the active-expiry design, idle-timeout rotation is handled
    automatically by expire_session_after_timeout, so we only need
    two cases here:
      1. No session exists yet → create one.
      2. Session exists and is still active → return it.
    """

    existing = _sessions.get(user_id)
 
    if existing is not None:
        # Already in memory — just refresh the timestamp.
        existing.last_interaction_at = time.time()
        return existing.session_id, False
 
    # Not in memory — check the DB.
    persisted = await load_session(user_id)
 
    if persisted is not None:
        elapsed = time.time() - persisted.last_interaction_at
        remaining = (IDLE_MINUTES * 60) - elapsed

        # Expired during downtime — delete and treat as new session
        if remaining <= 0:
            await delete_session(user_id)
            log.info(
                "Session expired during downtime",
                user_id=user_id,
                user_name=user_name,
                session_id=persisted.session_id
            )
        else:
            # Still valid — restore it
            session = UserSession(
                session_id=persisted.session_id,
                user_name=persisted.user_name,
                started_at=persisted.started_at,
                last_interaction_at=time.time(),
            )
            _sessions[user_id] = session
            log.info(
                "Restored session",
                user_id=user_id,
                user_name=user_name,
                session_id=persisted.session_id
            )
            return session.session_id, False

    # Genuinely new user — create a fresh session (existing code below, unchanged)
    session = UserSession(
        session_id=str(uuid.uuid4()),
        user_name=user_name,
    )
    _sessions[user_id] = session
    return session.session_id, True


# ──────────────────────────────────────────────────────────────────────────────
# Telegram handler
# ──────────────────────────────────────────────────────────────────────────────
async def on_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    active_chat_id = update.effective_chat.id

    user      = update.effective_user
    user_id   = str(user.id)
    user_name = user.first_name
    text = update.message.text or context.user_data.pop("voice_text", "") or ""

    # ── Session: get or create ────────────────────────────────────────────────
    session_id, is_new_session = await get_or_create_session(user_id, user_name)
    session = _sessions[user_id]
    session.chat_id = update.effective_chat.id

    # Persist after every interaction (updates last_interaction_at)
    await save_session(user_id, session)

    # ── REJECT-WHILE-BUSY ─────────────────────────────────────────────────────
    # If a response is already being generated for this user, don't start
    # another LangGraph run. Tell them to wait or use /stop.
    if session.is_processing:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "⏳ I'm still working on your previous message.\n\n"
                "Please wait for it to finish — or send /stop if you'd like to cancel it."
            )
        )
        return

    # ── Reset cancel flag from any previous /stop ─────────────────────────────
    # Must happen before we set is_processing, so a stale cancel_requested
    # from a prior session doesn't immediately abort this new request.
    session.cancel_requested = False

    if is_new_session:
        # First message — start the expiry countdown
        session.expiry_task = asyncio.create_task(
            expire_session_after_timeout(user_id, session_id, user_name)
        )
        log.info(
            "New session created",
            user_id=user_id,
            user_name=user_name,
            session_id=session_id,
            started_at=format_time(session.started_at)
        )

    else:
        # Returning message — cancel old timer, restart it fresh
        if session.expiry_task and not session.expiry_task.done():
            session.expiry_task.cancel()

        session.expiry_task = asyncio.create_task(
            expire_session_after_timeout(user_id, session_id, user_name)
        )

    log.info(
    "Telegram message received",
        user_id=user_id,
        user_name=user_name,
        session_id=session_id,
        message=text,
        session_started_at=format_time(session.started_at)
    )

    # ── Mark busy ─────────────────────────────────────────────────────────────
    session.is_processing = True

    # ── Sending the user meaning and helper messages before final response ────
    thinking_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ Thinking..."
    )

    async def status_callback(tool_name: str):
        label = TOOL_LABELS.get(tool_name, f"🔧 Running {tool_name}...")
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=thinking_msg.message_id,
                text=label
            )
        except Exception:
            pass

    # ── Wrap send() in a Task so /stop can cancel it ──────────────────────────
    async def _run_send():
        return await send(
            text,
            session_id,
            status_callback=status_callback,
            cancel_check=lambda: session.cancel_requested,
        )
 
    task = asyncio.create_task(_run_send())
    session.active_task = task

    # ── Send to LangGraph ─────────────────────────────────────────────────────
    try:
        result = await task

        # Track token usage from the session state safely post-execution
        if not session.cancel_requested:
            try:
                current_graph = agent_module.graph
                if current_graph:
                    state = await current_graph.aget_state({"configurable": {"thread_id": session_id}})
                    for msg in state.values.get("messages", []):
                        if hasattr(msg, "usage_metadata") and msg.usage_metadata:
                            model_name = msg.response_metadata.get("model_name", "gpt-5.4-mini")
                            msg_id = getattr(msg, "id", None)
                            
                            from usage_tracker import record_usage
                            usage_meta = msg.usage_metadata
                            record_usage(
                                dimension="agent",
                                session_id=session_id,
                                model_name=model_name,
                                input_tokens=usage_meta.get("input_tokens", 0),
                                output_tokens=usage_meta.get("output_tokens", 0),
                                cached_input_tokens=usage_meta.get("input_token_details", {}).get("cache_read_tokens", 0),
                                message_id=msg_id
                            )
            except Exception as token_err:
                log.warning("Failed to collect agent token metrics", error=str(token_err))

        # Task completed normally — only reply if not cancelled.
        # (If cancel was requested mid-run, send() returns None result;
        #  we just silently drop it per spec.)
        if not session.cancel_requested:
            if result["interrupt"]:
                await send_markdown(
                    context.bot,
                    update.effective_chat.id,
                    result["interrupt"]
                )
            elif result["reply"]:
                await send_markdown(
                    context.bot,
                    update.effective_chat.id,
                    result["reply"]
                )
    except asyncio.CancelledError:
        # /stop fired — task was cancelled externally. Say nothing. Do nothing.
        log.info("Task cancelled", user_id=user_id, user_name=user_name)

    except Exception as e:
        log.exception("Error in send")
        if not session.cancel_requested:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Something went wrong. Please try again."
            )

    finally:
        # clearing processing state, regardless of how we got here.
        session.is_processing = False
        session.active_task = None

        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=thinking_msg.message_id
            )
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI lifespan
# ──────────────────────────────────────────────────────────────────────────────
def load_persisted_chat_id() -> int | None:
    try:
        return json.loads(CHAT_ID_FILE.read_text())["chat_id"]
    except Exception:
        return None
    

async def dispatch_recurring_task(task_id: str, task_text: str):
    session_id = str(uuid.uuid4())

    log.info("Recurring task dispatched", task_id=task_id, session_id=session_id)

    try:
        result = await send(task_text, session_id)
    except Exception as e:
        log.exception("Recurring task agent error", task_id=task_id)
        return

    reply = result.get("reply") or result.get("interrupt")
    if not reply:
        log.warning("No reply from agent", task_id=task_id)
        return

    if active_chat_id is None:
        log.warning("No active Telegram chat, reply dropped", task_id=task_id)
        return

    try:
        await send_markdown(telegram_app.bot, active_chat_id, reply)
        log.info("Recurring task reply sent", task_id=task_id)
    except Exception as e:
        log.exception("Failed to send recurring task reply", task_id=task_id)
    
    
@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    global active_chat_id

    # ── Init persistent storage ───────────────────────────────────────────────
    await init_db()

    persisted = await load_all_sessions()
    now = time.time()

    for uid, p in persisted.items():
        elapsed = now - p.last_interaction_at
        remaining = (IDLE_MINUTES * 60) - elapsed

        # Session should have expired while server was down — clean it up
        if remaining <= 0:
            await delete_session(uid)
            log.warning(
                "Session expired during downtime, skipped restore",
                user_id=uid, 
                user_name=p.user_name, 
                session_id=p.session_id
            )
            continue

        # Session is still valid — restore it with the remaining time
        session = UserSession(
            session_id=p.session_id,
            user_name=p.user_name,
            started_at=p.started_at,
            last_interaction_at=p.last_interaction_at,
        )
        _sessions[uid] = session

        # Start expiry timer with REMAINING time, not full IDLE_MINUTES
        session.expiry_task = asyncio.create_task(
            expire_session_after_timeout(uid, p.session_id, p.user_name, override_seconds=remaining)
        )

        log.info(
            "Session restored",
            user_id=uid,
            user_name=p.user_name,
            session_id=p.session_id,
            expires_in_seconds=round(remaining),
        )

    if persisted:
        log.info("Sessions restored from DB", count=len(_sessions))

    saved = load_persisted_chat_id()
    if saved:
        active_chat_id = saved
        log.info("Loaded persisted chat id", chat_id=active_chat_id)
    else:
        log.warning("No chat id available yet")

    telegram_app = Application.builder().token(TOKEN).concurrent_updates(True).build()

    setup_command_handlers(telegram_app)

    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_telegram_message))
    telegram_app.add_handler(MessageHandler(filters.VOICE, on_voice_message))

    await telegram_app.initialize()
    await telegram_app.start()
    await setup_bot_commands(telegram_app)
    await telegram_app.updater.start_polling()

    log.info("Telegram bot is running...")

    if active_chat_id is not None:
        try:
            await telegram_app.bot.send_message(
                chat_id=active_chat_id,
                text="Sicily is awake and ready!"
            )
        except Exception:
            log.exception("Failed to send startup notification")

    set_dispatch(dispatch_recurring_task)

    # Do NOT await this here, or startup deadlocks.
    asyncio.create_task(initialize_agent())
    asyncio.create_task(start_recurring_tasks())

    # Reconnect whatever MCP connectors the user had turned on before the
    # last restart (swiggy, gmail, telegram, tavily, github, ...). This
    # must run AFTER initialize_agent has at least started, since it needs
    # agent_module.tool_manager to exist. It's fire-and-forget + best-effort
    # per connector, so one bad token doesn't block startup or the others.
    asyncio.create_task(restore_connectors_after_agent_ready())

    yield

    # ── Graceful drain on shutdown ────────────────────────────────────────────
    # 1. Notify every user who is mid-process.
    # 2. Give in-flight tasks up to GRACEFUL_DRAIN_TIMEOUT seconds to finish.
    # 3. Force-cancel anything still running after the timeout.
 
    processing_sessions = [
        (uid, s) for uid, s in _sessions.items() if s.is_processing
    ]
 
    if processing_sessions:
        log.warning("Shutdown with active sessions", active_sessions=len(processing_sessions))
 
        notify_tasks = []
        for uid, session in processing_sessions:
            # Best-effort notification — if Telegram is also down this will just fail silently.
            async def _notify(s=session):
                try:
                    # We need the chat_id for this user. We track active_chat_id globally
                    # (last active), but for a multi-user bot we need per-user chat ids.
                    # For now we use active_chat_id as a best effort; see note below.
                    if active_chat_id:
                        await telegram_app.bot.send_message(
                            chat_id=s.chat_id,
                            text=(
                                "⚠️ It looks like our connection was interrupted.\n\n"
                                "I wasn't able to finish processing your request. "
                                "Please try again in a moment — I'll be right back."
                            )
                        )
                except Exception as e:
                    log.warning("Could not notify user", user_name=s.user_name, error=str(e))
 
            notify_tasks.append(asyncio.create_task(_notify()))
 
        # Wait for all notifications to go out before cancelling tasks
        await asyncio.gather(*notify_tasks, return_exceptions=True)
 
        # Collect active tasks to drain
        active_tasks = [
            s.active_task
            for _, s in processing_sessions
            if s.active_task and not s.active_task.done()
        ]
 
        if active_tasks:
            log.info("Waiting for task drain", timeout_seconds=GRACEFUL_DRAIN_TIMEOUT, task_count=len(active_tasks))
            _, pending = await asyncio.wait(active_tasks, timeout=GRACEFUL_DRAIN_TIMEOUT)
 
            if pending:
                log.warning("Force cancelling tasks", task_count=len(pending))
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    # ── Cleanup: cancel all pending expiry tasks on shutdown ──────────────────
    for uid, session in _sessions.items():
        if session.expiry_task and not session.expiry_task.done():
            session.expiry_task.cancel()

    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


async def restore_connectors_after_agent_ready():
    """
    Waits for agent_module.tool_manager to exist (initialize_agent runs
    concurrently and creates it), then reconnects every connector the
    user had previously turned on.
    """
    for _ in range(100):  # ~10s max wait, in 0.1s steps
        if getattr(agent_module, "tool_manager", None) is not None:
            break
        await asyncio.sleep(0.1)
    else:
        log.warning("tool_manager_never_ready, skipping connector restore")
        return

    await restore_connected_connectors(agent_module.tool_manager)


async def on_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)
    audio_bytes = await tg_file.download_as_bytearray()

    client = get_transcriber()
    transcription = await client.audio.transcriptions.create(
        model="gpt-4o-mini-transcribe",
        file=("voice.ogg", bytes(audio_bytes), "audio/ogg"),
    )
    text = transcription.text.strip()

    log.info("Voice note transcribed", text=text)

    context.user_data["voice_text"] = text
    await on_telegram_message(update, context)


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {
        "status": "running",
        "chat_id": active_chat_id,
        "active_sessions": len(_sessions)
    }


@app.get("/callback")
async def oauth_callback(code: str, state: str | None = None):
    log.info("OAuth callback received")

    if Auth.swiggy_auth._oauth_code_future and not Auth.swiggy_auth._oauth_code_future.done():
        Auth.swiggy_auth._oauth_code_future.set_result(code)

    return {
        "status": "success",
        "message": "OAuth completed. You can close this tab."
    }


@app.post("/send")
async def send_to_telegram(text: str):
    """
    Send message to the last active Telegram chat.
    """

    if active_chat_id is None:
        return {"error": "No active chat yet — send a message from Telegram first"}

    await send_markdown(telegram_app.bot, active_chat_id, text)

    log.info("Message sent to Telegram", text=text)

    return {"status": "sent", "text": text}


# Entry
def main():
    init_settings()
    log.info("Sicily started successfully.")
    uvicorn.run(app, host="0.0.0.0", port=8000)


def init_settings():
    """Deprecated — now handled by configuration.load_config()"""
    pass


if __name__ == "__main__":
    main()
