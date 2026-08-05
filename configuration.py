from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI

import json
import os
from pathlib import Path
import structlog
from dotenv import load_dotenv

log = structlog.get_logger()

SICILY_HOME = Path.home() / ".sicily"
SETTINGS_PATH = SICILY_HOME / "settings.json"
ENV_PATH = SICILY_HOME / ".env"

REQUIRED_KEYS = [
    "OPENAI_API_KEY", 
    "TELEGRAM_BOT_TOKEN", 
    "TAVILY_API_KEY", 
    "GITHUB_TOKEN", 
    "NOTION_TOKEN", 
    "SPOTIFY_CLIENT_ID", 
    "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REDIRECT_URI",
]


def ensure_settings() -> bool:
    """Create settings.json from example if it doesn't exist."""
    if SETTINGS_PATH.exists():
        return False

    package_dir = Path(__file__).resolve().parent
    src = package_dir / "settings.example.json"

    if not src.exists():
        log.error("settings.example.json not found in package")
        return False

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(src, SETTINGS_PATH)
    log.info("Created default settings.json", path=str(SETTINGS_PATH))
    return True


def load_settings_to_env() -> None:
    """Load settings.json into os.environ and sync .env file."""
    if not SETTINGS_PATH.exists():
        raise FileNotFoundError(
            f"settings.json not found at {SETTINGS_PATH}. Run `sicily init` first."
        )

    with open(SETTINGS_PATH) as f:
        settings = json.load(f)

    # settings.json takes precedence
    for key in REQUIRED_KEYS:
        if val := settings.get(key):
            os.environ[key] = val

    # Sync .env file
    existing = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

    # Update with values from settings.json
    for key in REQUIRED_KEYS:
        if val := settings.get(key):
            existing[key] = val

    # Write back
    env_content = "\n".join(f"{k}={v}" for k, v in existing.items())
    ENV_PATH.write_text(env_content + "\n")

    log.debug("Environment synced from settings.json")


def load_config() -> None:
    """Main configuration loader — call this at the very top of entrypoints."""
    ensure_settings()
    load_settings_to_env()
    load_dotenv(ENV_PATH)
    log.debug("Full configuration loaded")


def get_main_llm(tools=None):
    llm = ChatOpenAI(model="gpt-5.4-mini")

    if tools:
        return llm.bind_tools(tools, parallel_tool_calls=False)
    return llm


def get_cowork_llm(tools=None):
    llm = ChatOpenAI(model="gpt-5.4-mini")

    if tools:
        return llm.bind_tools(tools, parallel_tool_calls=False)
    return llm


def get_safety_llm(schema):
    return ChatOpenAI(model="gpt-5.4-nano").with_structured_output(schema, include_raw=False)


def get_intent_llm(schema):
    return ChatOpenAI(model="gpt-5.4-nano").with_structured_output(schema, include_raw=False)


def get_eval_llm():
    return ChatOpenAI(model="gpt-4o-mini", temperature=0)


def get_summarizer_llm():
    return ChatOpenAI(model="gpt-4o-mini", temperature=0)


def get_transcriber() -> AsyncOpenAI:
    """Returns an AsyncOpenAI client for voice transcription via gpt-4o-mini-transcribe."""
    return AsyncOpenAI()


def navigator_smart_llm(schema=None):
    llm = ChatOpenAI(model="gpt-5.4-mini")
    
    if schema:
        return llm.with_structured_output(schema, include_raw=False)
    return llm


def navigator_general_llm(schema=None):
    llm = ChatOpenAI(model="gpt-5.4-nano")
    
    if schema:
        return llm.with_structured_output(schema, include_raw=False)
    return llm


TOOL_LABELS = {
    # ── Instamart: Discover ──────────────────────────────────
    "search_products":    "🔍 Searching for products...",
    "your_go_to_items":   "⭐ Fetching your go-to items...",
    "get_addresses":      "📍 Fetching your saved addresses...",
    "create_address":     "📍 Saving new address...",
    "delete_address":     "🗑️ Deleting address...",

    # ── Instamart: Cart ──────────────────────────────────────
    "get_cart":           "🛒 Fetching your cart...",
    "update_cart":        "🛒 Updating your cart...",
    "clear_cart":         "🗑️ Clearing your cart...",

    # ── Instamart: Order ─────────────────────────────────────
    "checkout":           "📦 Placing your order...",

    # ── Instamart: Track ─────────────────────────────────────
    "get_orders":         "📋 Fetching your order history...",
    "get_order_details":  "🔎 Getting order details...",
    "track_order":        "🚴 Tracking your order...",

    # ── Instamart: Support ───────────────────────────────────
    "report_error":       "📝 Generating error report...",

    # ── Food: Discover ───────────────────────────────────────
    "search_restaurants": "🔍 Searching restaurants...",
    "search_menu":        "🍽️ Searching the menu...",
    "get_restaurant_menu":"🍽️ Fetching restaurant menu...",

    # ── Food: Cart ───────────────────────────────────────────
    "get_food_cart":      "🛒 Fetching your food cart...",
    "update_food_cart":   "🛒 Updating your food cart...",
    "flush_food_cart":    "🗑️ Clearing your food cart...",
    "fetch_food_coupons": "🎟️ Finding available coupons...",
    "apply_food_coupon":  "🎟️ Applying coupon...",

    # ── Food: Order ──────────────────────────────────────────
    "place_food_order":   "📦 Placing your food order...",

    # ── Food: Track ──────────────────────────────────────────
    "get_food_orders":    "📋 Fetching your food orders...",
    "get_food_order_details": "🔎 Getting order details...",
    "track_food_order":   "🚴 Tracking your food order...",

    # ── Gmail ────────────────────────────────────────────────
    "create_draft":       "✉️ Creating email draft...",
    "list_drafts":        "📄 Fetching email drafts...",
    "get_thread":         "📧 Loading email conversation...",
    "search_threads":     "🔍 Searching emails...",
    "label_thread":       "🏷️ Updating conversation labels...",
    "unlabel_thread":     "🏷️ Removing conversation labels...",
    "list_labels":        "📋 Fetching email labels...",
    "label_message":      "🏷️ Updating email labels...",
    "unlabel_message":    "🏷️ Removing email labels...",
    "create_label":       "➕ Creating email label...",
    "update_label":       "✏️ Updating email label...",
    "delete_label":       "🗑️ Deleting email label...",

    # ── Telegram: Profile & Account ──────────────────────────
    "get_me":                     "👤 Fetching profile details...",

    # ── Telegram: Contacts ───────────────────────────────────
    "list_contacts":              "👥 Fetching contact list...",
    "search_contacts":            "🔍 Searching contacts...",
    "get_contact_ids":            "🆔 Fetching contact IDs...",
    "get_direct_chat_by_contact":  "💬 Opening contact chat...",
    "get_contact_chats":          "💬 Finding chats with contact...",
    "get_last_interaction":       "⏳ Checking last interaction time...",
    "add_contact":                "➕ Adding new contact...",
    "delete_contact":             "🗑️ Deleting contact...",
    "block_user":                 "🚫 Blocking user...",
    "unblock_user":               "✅ Unblocking user...",
    "get_blocked_users":          "📋 Fetching blocked users list...",

    # ── Telegram: Chats & Channels ───────────────────────────
    "get_chats":                  "💬 Loading recent chats...",
    "list_chats":                 "📋 Listing available chats...",
    "get_chat":                   "💬 Fetching chat information...",
    "resolve_username":           "🔎 Resolving Telegram username...",
    "archive_chat":               "📦 Moving chat to archive...",
    "unarchive_chat":             "📤 Removing chat from archive...",

    # ── Telegram: Messages & History ─────────────────────────
    "get_messages":               "💬 Loading specific messages...",
    "send_message":               "💬 Sending message...",
    "send_scheduled_message":     "⏰ Scheduling message...",
    "get_scheduled_messages":     "📋 Fetching scheduled messages...",
    "delete_scheduled_message":   "🗑️ Deleting scheduled message...",
    "list_messages":              "📋 Listing chat messages...",
    "get_message_context":        "🔍 Loading surrounding message context...",
    "edit_message":               "✏️ Editing message...",
    "delete_message":             "🗑️ Deleting message...",
    "delete_chat_history":        "🧹 Wiping chat history...",
    "delete_messages_bulk":       "🗑️ Deleting multiple messages...",
    "mark_as_read":               "✔️ Marking messages as read...",
    "reply_to_message":           "💬 Replying to message...",
    "search_messages":            "🔍 Searching messages in chat...",
    "get_history":                "📜 Loading chat history...",
    "save_draft":                 "📝 Saving message draft...",
    "get_drafts":                 "📄 Fetching message drafts...",
    "clear_draft":                "🗑️ Clearing message draft...",

    # ── Spotify ──────────────────────────────────────────────
    "SpotifyPlayback":    "🎵 Controlling Spotify playback...",
    "SpotifySearch":      "🔍 Searching Spotify...",
    "SpotifyQueue":       "🎶 Managing Spotify queue...",
    "SpotifyGetInfo":     "ℹ️ Fetching Spotify details...",
    "SpotifyPlaylist":    "📂 Managing Spotify playlists...",
}


TELEGRAM_BLACKLIST = {
    # Account & Profile Management
    "list_accounts", "update_profile", "set_profile_photo", "delete_profile_photo", 
    "get_user_photos", "get_user_status", "get_privacy_settings", "set_privacy_settings", "get_full_user",
    
    # Contact Operations
    "import_contacts", "export_contacts", "send_contact",
    
    # Advanced Chat Metadata & Public Actions
    "get_full_chat", "search_public_chats", "subscribe_public_channel", 
    "leave_chat", "mute_chat", "unmute_chat", "get_common_chats", "list_topics",

    # Group & Channel Administration 
    "create_group", "invite_to_group", "get_participants", "create_channel", 
    "edit_chat_title", "edit_chat_photo", "edit_chat_about", "delete_chat_photo", 
    "promote_admin", "demote_admin", "ban_user", "unban_user", 
    "set_default_chat_permissions", "toggle_slow_mode", "edit_admin_rights", 
    "get_admins", "get_banned_users", "get_invite_link", "join_chat_by_link", 
    "export_chat_invite", "import_chat_invite", "get_recent_actions",

    # Unnecessary Message Interactions
    "get_message_read_by", "get_message_link", "list_inline_buttons", 
    "press_inline_button", "forward_message", "forward_messages", "pin_message", 
    "unpin_message", "unpin_all_messages",

    # Broad Scope Searches & Reactions
    "search_global", "get_pinned_messages", "create_poll", "send_reaction", 
    "remove_reaction", "get_message_reactions",

    # Media & Attachments
    "send_file", "send_album", "download_media", "send_voice", "upload_file", 
    "get_media_info", "get_sticker_sets", "send_sticker", "get_gif_search", "send_gif",

    # Chat Folders
    "list_folders", "get_folder", "create_folder", "add_chat_to_folder", 
    "remove_chat_from_folder", "delete_folder", "reorder_folders",

    # Bots & Real-Time Operations
    "get_bot_info", "set_bot_commands", "wait_for_new_message", "wait_for_settled_message"
}
