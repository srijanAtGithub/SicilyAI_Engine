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


def navigator_basic_llm(schema=None):
    llm = ChatOpenAI(model="gpt-5-nano")
    
    if schema:
        return llm.with_structured_output(schema, include_raw=False)
    return llm


TOOL_LABELS = {
    # ── Swiggy Instamart ──────────────────────────────────
    "search_products":    "🔍 Searching for products...",
    "your_go_to_items":   "⭐ Fetching your go-to items...",
    "get_addresses":      "📍 Fetching your saved addresses...",
    "create_address":     "📍 Saving new address...",
    "delete_address":     "🗑️ Deleting address...",
    "get_cart":           "🛒 Fetching your cart...",
    "update_cart":        "🛒 Updating your cart...",
    "clear_cart":         "🗑️ Clearing your cart...",
    "checkout":           "📦 Placing your order...",
    "get_orders":         "📋 Fetching your order history...",
    "get_order_details":  "🔎 Getting order details...",
    "track_order":        "🚴 Tracking your order...",
    "report_error":       "📝 Generating error report...",

    # ── Swiggy Food ───────────────────────────────────────
    "search_restaurants": "🔍 Searching restaurants...",
    "search_menu":        "🍽️ Searching the menu...",
    "get_restaurant_menu":"🍽️ Fetching restaurant menu...",
    "get_food_cart":      "🛒 Fetching your food cart...",
    "update_food_cart":   "🛒 Updating your food cart...",
    "flush_food_cart":    "🗑️ Clearing your food cart...",
    "fetch_food_coupons": "🎟️ Finding available coupons...",
    "apply_food_coupon":  "🎟️ Applying coupon...",
    "place_food_order":   "📦 Placing your food order...",
    "get_food_orders":    "📋 Fetching your food orders...",
    "get_food_order_details": "🔎 Getting order details...",
    "track_food_order":   "🚴 Tracking your food order...",

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

    # ── Telegram: Chats & Channels ─────────────────────────
    "get_chats":                  "💬 Loading recent chats...",
    "list_chats":                 "📋 Listing available chats...",
    "get_chat":                   "💬 Fetching chat information...",
    "resolve_username":           "🔎 Resolving Telegram username...",
    "archive_chat":               "📦 Moving chat to archive...",
    "unarchive_chat":             "📤 Removing chat from archive...",

    # ── Telegram: Messages & History ───────────────────────
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

    # ── Tavily ──────────────────────────────────────────────
    "tavily_search":   "🔍 Searching the web with Tavily...",
    "tavily_extract":  "📄 Extracting web content...",
    "tavily_crawl":    "🕷️ Crawling web pages...",
    "tavily_map":      "🗺️ Mapping site structure...",
    "tavily_research": "🔬 Conducting web research...",

    # ── GitHub ───────────────────────────────────────────────
    "add_comment_to_pending_review":     "💬 Commenting on PR review...",
    "add_issue_comment":                 "💬 Commenting on issue...",
    "add_reply_to_pull_request_comment": "💬 Replying to PR comment...",
    "create_branch":                     "🌿 Creating Git branch...",
    "create_or_update_file":             "📝 Updating file on GitHub...",
    "create_pull_request":               "🔀 Creating pull request...",
    "create_repository":                 "📦 Creating GitHub repository...",
    "delete_file":                       "🗑️ Deleting file on GitHub...",
    "fork_repository":                   "🍴 Forking repository...",
    "get_commit":                        "📜 Fetching commit details...",
    "get_file_contents":                 "📄 Reading file contents...",
    "get_label":                         "🏷️ Fetching GitHub label...",
    "get_latest_release":                "🚀 Fetching latest release...",
    "get_me":                            "👤 Fetching GitHub user profile...",
    "get_release_by_tag":                "🏷️ Fetching release by tag...",
    "get_tag":                           "🏷️ Fetching Git tag...",
    "get_team_members":                  "👥 Fetching team members...",
    "get_teams":                         "👥 Fetching GitHub teams...",
    "issue_read":                        "👁️ Reading GitHub issue...",
    "issue_write":                       "✏️ Updating GitHub issue...",
    "list_branches":                     "🌿 Listing repository branches...",
    "list_commits":                      "📜 Listing commits...",
    "list_issue_fields":                 "📋 Listing issue fields...",
    "list_issue_types":                  "📋 Listing issue types...",
    "list_issues":                       "📋 Listing GitHub issues...",
    "list_pull_requests":                "🔀 Listing pull requests...",
    "list_releases":                     "🚀 Listing releases...",
    "list_repository_collaborators":     "👥 Listing collaborators...",
    "list_tags":                         "🏷️ Listing repository tags...",
    "merge_pull_request":                "🔀 Merging pull request...",
    "pull_request_read":                 "👁️ Reading pull request...",
    "pull_request_review_write":        "✏️ Updating PR review...",
    "push_files":                        "⬆️ Pushing files to GitHub...",
    "request_copilot_review":            "🤖 Requesting Copilot review...",
    "run_secret_scanning":               "🛡️ Running secret scanning...",
    "search_code":                       "🔍 Searching code on GitHub...",
    "search_commits":                    "🔍 Searching commits...",
    "search_issues":                     "🔍 Searching GitHub issues...",
    "search_pull_requests":              "🔍 Searching pull requests...",
    "search_repositories":               "🔍 Searching repositories...",
    "search_users":                      "🔍 Searching GitHub users...",
    "sub_issue_write":                   "✏️ Updating sub-issue...",
    "update_pull_request":               "✏️ Updating pull request...",
    "update_pull_request_branch":        "🌿 Updating PR branch...",

    # ── Notion ───────────────────────────────────────────────
    "notion_execute":  "📝 Executing Notion operation...",
    "notion_describe": "ℹ️ Describing Notion schema...",

    # ── Spotify ──────────────────────────────────────────────
    "SpotifyPlayback":            "🎵 Controlling Spotify playback...",
    "SpotifySearch":              "🔍 Searching Spotify...",
    "SpotifyQueue":               "🎶 Managing Spotify queue...",
    "SpotifyGetInfo":             "ℹ️ Fetching Spotify details...",
    "SpotifyPlaylist":            "📂 Managing Spotify playlists...",

    # ── Canva ────────────────────────────────────────────────
    "create-folder":                     "📁 Creating Canva folder...",
    "list-folder-items":                 "📂 Listing folder items...",
    "move-item-to-folder":               "📦 Moving item to folder...",
    "search-folders":                    "🔍 Searching Canva folders...",
    "export-design":                     "📤 Exporting Canva design...",
    "get-export-formats":                "⚙️ Fetching export formats...",
    "comment-on-design":                 "💬 Commenting on design...",
    "list-comments":                     "💬 Fetching design comments...",
    "list-replies":                      "💬 Fetching comment replies...",
    "reply-to-comment":                  "💬 Replying to comment...",
    "get-design":                        "🎨 Fetching Canva design...",
    "get-design-pages":                  "📄 Fetching design pages...",
    "get-design-content":                "📝 Fetching design content...",
    "get-presenter-notes":               "🗒️ Fetching presenter notes...",
    "search-designs":                    "🔍 Searching Canva designs...",
    "copy-design":                       "📋 Copying Canva design...",
    "create-design-from-brand-template": "✨ Creating design from brand template...",
    "import-design-from-url":            "📥 Importing design from URL...",
    "upload-asset-from-url":             "⬆️ Uploading asset from URL...",
    "resize-design":                     "📐 Resizing Canva design...",
    "start-editing-transaction":         "✏️ Starting edit session...",
    "perform-editing-operations":        "⚙️ Performing design edits...",
    "commit-editing-transaction":        "💾 Saving design edits...",
    "cancel-editing-transaction":        "❌ Canceling design edits...",
    "get-design-thumbnail":              "🖼️ Fetching design thumbnail...",
    "search-brand-templates":            "🔍 Searching brand templates...",
    "get-brand-template-dataset":        "📊 Fetching brand template data...",
    "resolve-shortlink":                 "🔗 Resolving Canva shortlink...",
    "get-assets":                        "🖼️ Fetching assets...",
    "list-brand-kits":                   "🎨 Listing brand kits...",
    "get-design-candidates":             "✨ Fetching design candidates...",
    "create-design-from-candidate":      "🎨 Creating design from candidate...",
    "generate-design":                   "✨ Generating Canva design...",

    # ── Excalidraw ───────────────────────────────────────────
    "read_me":                           "📖 Reading Excalidraw info...",
    "create_view":                       "🖍️ Creating Excalidraw view...",
    "export_to_excalidraw":              "📤 Exporting to Excalidraw...",
    "save_checkpoint":                   "💾 Saving Excalidraw checkpoint...",
    "read_checkpoint":                   "📂 Loading Excalidraw checkpoint...",
    
    # ── Linear ───────────────────────────────────────────────
    "get_attachment":                    "📎 Fetching attachment...",
    "prepare_attachment_upload":         "⬆️ Preparing attachment upload...",
    "create_attachment_from_upload":     "📎 Creating attachment from upload...",
    "create_attachment":                 "📎 Creating attachment...",
    "delete_attachment":                 "🗑️ Deleting attachment...",
    "list_agent_skills":                 "🤖 Listing agent skills...",
    "get_agent_skill":                   "🤖 Fetching agent skill...",
    "list_comments":                     "💬 Listing comments...",
    "save_comment":                      "💬 Saving comment...",
    "delete_comment":                    "🗑️ Deleting comment...",
    "list_cycles":                       "🔄 Listing cycles...",
    "get_document":                      "📄 Fetching document...",
    "list_documents":                    "📄 Listing documents...",
    "save_document":                     "💾 Saving document...",
    "extract_images":                    "🖼️ Extracting images...",
    "get_issue":                         "🎫 Fetching issue...",
    "list_issues":                       "📋 Listing issues...",
    "save_issue":                        "💾 Saving issue...",
    "list_issue_statuses":               "🚥 Listing issue statuses...",
    "get_issue_status":                  "🚥 Fetching issue status...",
    "list_issue_labels":                 "🏷️ Listing issue labels...",
    "create_issue_label":                "🏷️ Creating issue label...",
    "list_projects":                     "📁 Listing projects...",
    "get_project":                       "📁 Fetching project...",
    "save_project":                      "💾 Saving project...",
    "list_project_labels":               "🏷️ Listing project labels...",
    "list_release_pipelines":            "🚀 Listing release pipelines...",
    "list_releases":                     "🚀 Listing releases...",
    "get_release":                       "🚀 Fetching release...",
    "save_release":                      "💾 Saving release...",
    "list_release_notes":                "📝 Listing release notes...",
    "get_release_note":                  "📝 Fetching release note...",
    "save_release_note":                 "💾 Saving release note...",
    "get_diff":                          "📝 Fetching diff...",
    "list_diffs":                        "📝 Listing diffs...",
    "get_diff_threads":                  "💬 Fetching diff threads...",
    "save_diff_comment":                 "💬 Saving diff comment...",
    "resolve_diff_thread":               "✅ Resolving diff thread...",
    "delete_diff_comment":               "🗑️ Deleting diff comment...",
    "submit_diff_review":                "✅ Submitting diff review...",
    "merge_diff":                        "🔀 Merging diff...",
    "list_milestones":                   "🎯 Listing milestones...",
    "get_milestone":                     "🎯 Fetching milestone...",
    "save_milestone":                    "💾 Saving milestone...",
    "list_teams":                        "👥 Listing teams...",
    "get_team":                          "👥 Fetching team...",
    "list_users":                        "👤 Listing users...",
    "get_user":                          "👤 Fetching user...",
    "search_documentation":              "🔍 Searching documentation...",
    "get_status_updates":                "🚥 Fetching status updates...",
    "save_status_update":                "💾 Saving status update...",
    "delete_status_update":              "🗑️ Deleting status update...",

    # ── Google Calendar ───────────────────────────────────────────────
    "list-calendars":                    "📅 Listing calendars...",
    "list-events":                       "📆 Listing events...",
    "search-events":                     "🔍 Searching events...",
    "get-event":                         "📆 Fetching event...",
    "list-colors":                       "🎨 Listing colors...",
    "create-event":                      "📅 Creating event...",
    "create-events":                     "📅 Creating events...",
    "update-event":                      "✏️ Updating event...",
    "delete-event":                      "🗑️ Deleting event...",
    "get-freebusy":                      "📊 Fetching availability...",
    "get-current-time":                  "⏰ Fetching current time...",
    "respond-to-event":                  "✉️ Responding to event...",
    "manage-accounts":                   "⚙️ Managing accounts...",
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
