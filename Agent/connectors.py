import os
import json
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient
from Auth.swiggy_auth import get_swiggy_token
from Auth.gmail_auth import get_gmail_token
from Auth.telegram_auth import get_telegram_config
from Auth.tavily_auth import get_tavily_config
from Auth.github_auth import get_github_config
from Auth.notion_auth import get_notion_config
from Auth.spotify_auth import get_spotify_config

from configuration import TELEGRAM_BLACKLIST

import structlog
log = structlog.get_logger()

SICILY_HOME = Path.home() / ".sicily"
CONNECTED_PATH = SICILY_HOME / "connected.json"

async def load_swiggy_tools(tool_manager):
    token = await get_swiggy_token()

    # return await client.get_tools()
    food_client = MultiServerMCPClient({
        "swiggy-food": {
            "transport": "streamable_http",
            "url": "https://mcp.swiggy.com/food",
            "headers": {"Authorization": f"Bearer {token}"},
        }
    })
    instamart_client = MultiServerMCPClient({
        "swiggy-instamart": {
            "transport": "streamable_http",
            "url": "https://mcp.swiggy.com/im",
            "headers": {"Authorization": f"Bearer {token}"},
        }
    })

    food_tools = await food_client.get_tools()
    im_tools   = await instamart_client.get_tools()

    await tool_manager.register(food_tools, "swiggy-food")
    await tool_manager.register(im_tools, "swiggy-instamart")


async def load_gmail_tools(tool_manager):
    token = await get_gmail_token()

    gmail_client = MultiServerMCPClient({
        "gmail": {
            "transport": "streamable_http",
            "url": "https://gmailmcp.googleapis.com/mcp/v1",
            "headers": {"Authorization": f"Bearer {token}"},
        }
    })

    tools = await gmail_client.get_tools()
    await tool_manager.register(tools, "gmail")


async def load_telegram_tools(tool_manager):
    env = await get_telegram_config()

    telegram_client = MultiServerMCPClient({
        "telegram": {
            "transport": "stdio",
            "command": "uv",
            "args": [
                "--directory", "/Users/srijan/MCP Servers/Telegram MCP Server",
                "run",
                "main.py",
            ],
            "env": env,
        }
    })

    raw_tools = await telegram_client.get_tools()
    # excluding blacklisted tools
    filtered_tools = [tool for tool in raw_tools if tool.name not in TELEGRAM_BLACKLIST]
    await tool_manager.register(filtered_tools, "telegram")


async def load_tavily_tools(tool_manager):
    env = await get_tavily_config()
    api_key = env["TAVILY_API_KEY"]

    tavily_client = MultiServerMCPClient({
        "tavily": {
            "transport": "streamable_http",
            "url": f"https://mcp.tavily.com/mcp/?tavilyApiKey={api_key}",
        }
    })
    tools = await tavily_client.get_tools()
    await tool_manager.register(tools, "tavily")


async def load_github_tools(tool_manager):
    env = await get_github_config()
    token = env["GITHUB_TOKEN"]

    github_client = MultiServerMCPClient({
        "github": {
            "transport": "streamable_http",
            "url": "https://api.githubcopilot.com/mcp/",
            "headers": {"Authorization": f"Bearer {token}"},
        }
    })
    tools = await github_client.get_tools()
    await tool_manager.register(tools, "github")


async def load_notion_tools(tool_manager):
    env_vars = await get_notion_config()
    env = os.environ.copy()
    env.update(env_vars)

    notion_client = MultiServerMCPClient({
        "notion": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "notion-mcp-server"],
            "env": env,
        }
    })

    tools = await notion_client.get_tools()
    await tool_manager.register(tools, "notion")


async def load_spotify_tools(tool_manager):
    env_vars = await get_spotify_config()
    env = os.environ.copy()
    env.update(env_vars)

    spotify_client = MultiServerMCPClient({
        "spotify": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@darrenjaws/spotify-mcp"],
            "env": env,
        }
    })

    tools = await spotify_client.get_tools()
    await tool_manager.register(tools, "spotify")


# Registry of all available connectors — add new ones here
CONNECTORS = {
    "swiggy":      load_swiggy_tools,
    "gmail":       load_gmail_tools,
    "telegram":    load_telegram_tools,
    "tavily":      load_tavily_tools,
    "github":      load_github_tools,
    "notion":      load_notion_tools,
    "spotify":     load_spotify_tools,
}

# Some connectors register more than one MCP server under the hood
# (e.g. "swiggy" spins up both "swiggy-food" and "swiggy-instamart").
# This maps a connector name -> the tool_manager server name(s) it owns,
# so /connect_*, /disconnect_*, and the "is this loaded?" check all stay
# correct without needing a special case anywhere else.
# Any connector not listed here is assumed to register a server with the
# same name as the connector itself (the common case).
CONNECTOR_SERVERS = {
    "swiggy": ["swiggy-food", "swiggy-instamart"],
}


def get_connector_servers(name: str) -> list[str]:
    """Server name(s) a given connector registers with the tool_manager."""
    return CONNECTOR_SERVERS.get(name, [name])


def is_connector_loaded(name: str, loaded_servers) -> bool:
    """True if any server belonging to this connector is currently loaded."""
    return any(server in loaded_servers for server in get_connector_servers(name))


# ── Persistence: which connectors the user has turned on ────────────
#
# This does NOT persist the actual MCP tool objects/sessions (those are
# short-lived, carry live tokens/clients and must be re-fetched fresh
# every process start regardless). It only persists the *set of
# connector names* the user has previously connected, so we know what
# to reconnect to automatically on the next boot — instead of coming
# up with zero tools and silently waiting for the user to notice and
# re-run every /connect_* command by hand.

def _read_connected() -> set[str]:
    if not CONNECTED_PATH.exists():
        return set()
    try:
        data = json.loads(CONNECTED_PATH.read_text())
        return set(data.get("connectors", []))
    except Exception:
        log.warning("connected_json_unreadable, treating as empty")
        return set()


def _write_connected(names: set[str]) -> None:
    CONNECTED_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONNECTED_PATH.write_text(json.dumps({"connectors": sorted(names)}, indent=2))


def mark_connector_connected(name: str) -> None:
    """Call this right after a connector's load_* function succeeds."""
    names = _read_connected()
    if name not in names:
        names.add(name)
        _write_connected(names)
        log.info("connector_marked_persisted", connector=name)


def mark_connector_disconnected(name: str) -> None:
    """Call this from /disconnect_* so we don't try to reconnect it on next boot."""
    names = _read_connected()
    if name in names:
        names.discard(name)
        _write_connected(names)
        log.info("connector_unmarked_persisted", connector=name)


async def restore_connected_connectors(tool_manager) -> None:
    """
    Call this once at startup (after tool_manager exists, before/alongside
    initialize_agent). Reconnects every connector the user had previously
    turned on, using the SAME load_* functions /connect_* commands use —
    so auth/token fetching happens fresh, only the "which ones" list is
    persisted.

    Best-effort per connector: one connector failing to reconnect (e.g.
    expired token, MCP server down) must not block the others or crash
    startup — that would turn "some data reset" into "nothing works".
    """
    names = _read_connected()
    if not names:
        log.info("no_persisted_connectors_to_restore")
        return

    for name in sorted(names):
        loader = CONNECTORS.get(name)
        if loader is None:
            log.warning("persisted_connector_unknown_skipping", connector=name)
            continue
        try:
            await loader(tool_manager)
            log.info("connector_restored", connector=name)
        except Exception as e:
            log.warning("connector_restore_failed", connector=name, error=str(e))
            # Leave it marked as "connected" in connected.json — it was a
            # transient failure (bad token, MCP server down), not the user
            # disconnecting it. We'll just retry on the next restart.