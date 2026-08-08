import json
from telegram import Update, BotCommand
from telegram.ext import CommandHandler, MessageHandler, filters, ContextTypes

from Agent.agent import tool_manager
from Agent.connectors import (
    CONNECTORS,
    get_connector_servers,
    is_connector_loaded,
    mark_connector_connected,
    mark_connector_disconnected,
)


# TELEGRAM COMMANDS EXECUTORS
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import Agent.main

    Agent.main.active_chat_id = update.effective_chat.id

    if not Agent.main.CHAT_ID_FILE.exists():
        Agent.main.CHAT_ID_FILE.write_text(json.dumps({"chat_id": Agent.main.active_chat_id}))
        print(f"💾 Registered chat_id: {Agent.main.active_chat_id} for user {update.effective_user.first_name}")
        await update.message.reply_text(
            "👋 Hi! I'm Sicily. You're all set up.."
        )
    else:
        await update.message.reply_text("👋 Hey! Already registered. Ready to go.")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Cancel whatever is currently processing for this user.
    No reply to the user — just stop everything silently.
    """
    import Agent.main

    user_id = str(update.effective_user.id)
    session = Agent.main._sessions.get(user_id)
 
    if session is None or not session.is_processing:
        # Nothing running — silently do nothing.
        return
 
    print(
        f"\n🛑 /stop received"
        f"\n👤 User : {session.user_name} ({user_id})"
        f"\n🧵 Session: {session.session_id}\n"
    )
 
    # Signal the cancel_check lambda inside send()
    session.cancel_requested = True
 
    # Cancel the asyncio Task wrapping send()
    if session.active_task and not session.active_task.done():
        session.active_task.cancel()
 
    # No reply, no acknowledgement — per spec.


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import Agent.main
    from Agent.session_store import load_session

    user_id = str(update.effective_user.id)

    # Prefer the live in-memory session (has is_processing state)
    session = Agent.main._sessions.get(user_id)
    if session:
        await update.message.reply_text(
            f"🧵 Session ID: {session.session_id[:8]}...\n"
            f"🕒 Started: {Agent.main.format_time(session.started_at)}\n"
            f"💬 Last msg: {Agent.main.format_time(session.last_interaction_at)}\n"
            f"⚙️  Processing: {'Yes' if session.is_processing else 'No'}"
        )
        return

    # Fall back to the persisted DB record (e.g. after a restart, or before
    # the first regular message of this boot has been processed)
    persisted = await load_session(user_id)
    if persisted:
        await update.message.reply_text(
            f"🧵 Session ID: {persisted.session_id[:8]}...\n"
            f"🕒 Started: {Agent.main.format_time(persisted.started_at)}\n"
            f"💬 Last msg: {Agent.main.format_time(persisted.last_interaction_at)}\n"
            f"⚙️  Processing: No"
        )
    else:
        await update.message.reply_text("No active session.")


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from usage_tracker import get_usage_report, init_db, cleanup_old_records
    init_db()
    cleanup_old_records()

    timeframe = "week"
    title_suffix = "Last 7 Days"

    if context.args:
        arg = context.args[0].lower()
        if arg == "session":
            timeframe = "session"
            title_suffix = "Last Session"
        elif arg == "day":
            timeframe = "day"
            title_suffix = "Last 24 Hours"

    report = get_usage_report(timeframe=timeframe)
    if not report:
        await update.message.reply_text(f" No usage metrics discovered for: {title_suffix}")
        return

    text_lines = [f" *Sicily Usage Report ({title_suffix})*", ""]
    total_cost = 0.0

    for row in report:
        dim = row["dimension"].upper()
        model = row["model_name"]
        in_t = row["in_tokens"]
        out_t = row["out_tokens"]
        cost = row["total_cost"]
        total_cost += cost
        
        text_lines.append(
            f"▪️ *{dim}* — `{model}`\n"
            f"  Input: {in_t:,}\n"
            f"  Output: {out_t:,}\n"
            f"  Cost: ${cost:.5f}\n"
        )
    
    text_lines.append(f" *Total Estimated Cost: ${total_cost:.5f} USD*")
    await update.message.reply_text("\n".join(text_lines), parse_mode="Markdown")


# DYNAMIC CONNECTOR COMMANDS
#
# Instead of one CommandHandler per connector (which forces every
# /connect_x /disconnect_x pair to be hand-registered here and shown in the
# Telegram "/" menu forever), we:
#   1. Only ever register 5 static commands.
#   2. /connectors and /loaded_connectors render the relevant connector
#      names as "/connect_<name>" / "/disconnect_<name>" text — Telegram
#      auto-detects these as tappable commands even though they aren't in
#      the bot's command menu, so tapping one sends it straight to the chat.
#   3. A single generic handler below catches any /connect_* or
#      /disconnect_* command and dispatches to the right connector loader,
#      so adding a new entry to CONNECTORS in connectors.py is the only
#      change needed to support it end-to-end.

async def connectors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loaded = tool_manager.loaded_servers
    available = [name for name in CONNECTORS if not is_connector_loaded(name, loaded)]

    if not available:
        await update.message.reply_text("✅ All connectors are already connected!")
        return

    commands = "\n".join(f"/connect_{name}" for name in available)
    await update.message.reply_text(
        f"Available connectors:\n\n{commands}"
    )


async def loaded_connectors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loaded = tool_manager.loaded_servers
    connected = [name for name in CONNECTORS if is_connector_loaded(name, loaded)]

    if not connected:
        await update.message.reply_text("🔌 No connectors loaded currently.")
        return

    commands = "\n".join(f"/disconnect_{name}" for name in connected)
    await update.message.reply_text(
        f"Loaded connectors:\n\n{commands}"
    )


async def _handle_connect(update: Update, name: str):
    import os

    loaded = tool_manager.loaded_servers
    if is_connector_loaded(name, loaded):
        await update.message.reply_text(f"⚠️ {name.title()} is already connected.")
        return

    # Grab the actual loader function from the registry
    loader_func = CONNECTORS[name]
    
    # Dynamically read the required keys (defaults to [] if no decorator was used)
    required_keys = getattr(loader_func, "required_keys", [])
    
    missing_keys = [
        key for key in required_keys 
        if not os.getenv(key) or "your_" in os.getenv(key).lower()
    ]

    if missing_keys:
        keys_str = ", ".join(missing_keys)
        await update.message.reply_text(
            f"⚠️ Cannot connect to {name.title()}.\n\n"
            f"Please add your `{keys_str}` to your `settings.json` file first.\n"
            "You can open your configuration folder by running `sicily config` in your terminal."
        )
        return

    # Proceed with connection
    await update.message.reply_text(f"⏳ Connecting {name.title()}...")
    try:
        await loader_func(tool_manager)
        mark_connector_connected(name)
        await update.message.reply_text(f"✅ {name.title()} connected successfully!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to connect {name.title()}:\n{str(e)}")


async def _handle_disconnect(update: Update, name: str):
    loaded = tool_manager.loaded_servers
    if not is_connector_loaded(name, loaded):
        await update.message.reply_text(f"⚠️ {name.title()} isn't connected.")
        return

    for server in get_connector_servers(name):
        tool_manager.unregister(server)
    mark_connector_disconnected(name)  # so it's NOT auto-reconnected on next boot
    await update.message.reply_text(f"🗑️ {name.title()} disconnected.")


async def connector_command_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Catches every /command that isn't one of the 5 statically registered
    ones. Only /connect_<name> and /disconnect_<name> are recognized;
    anything else (typos, stale commands, etc.) is ignored silently.
    """
    raw = update.message.text or ""
    command = raw.split()[0][1:]           # strip leading "/"
    command = command.split("@")[0]        # strip "@BotName" (group chats)

    if command.startswith("connect_"):
        name = command[len("connect_"):]
        if name in CONNECTORS:
            await _handle_connect(update, name)
    elif command.startswith("disconnect_"):
        name = command[len("disconnect_"):]
        if name in CONNECTORS:
            await _handle_disconnect(update, name)
    # else: not a recognized command shape — ignore silently.


# REGISTRATION HELPERS
def setup_command_handlers(telegram_app):
    """Attaches all command handlers to the application."""
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("stop", stop_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("usage", usage_command))
    telegram_app.add_handler(CommandHandler("connectors", connectors_command))
    telegram_app.add_handler(CommandHandler("loaded_connectors", loaded_connectors_command))

    # Catch-all for /connect_* and /disconnect_* — must be added last so the
    # static commands above get first refusal within the handler group.
    telegram_app.add_handler(MessageHandler(filters.COMMAND, connector_command_dispatch))


async def setup_bot_commands(telegram_app):
    """Sets the UI menu commands in Telegram."""
    await telegram_app.bot.set_my_commands([
        BotCommand("start",             "Start the bot"),
        BotCommand("stop",              "Stop the current process"),
        BotCommand("status",            "Show your session info"),
        BotCommand("usage",             "Show token metrics and historical costs"),
        BotCommand("connectors",        "Show available connectors to connect"),
        BotCommand("loaded_connectors", "Show connected connectors (and disconnect them)"),
    ])