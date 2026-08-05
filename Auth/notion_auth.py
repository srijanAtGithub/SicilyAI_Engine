import os

async def get_notion_config() -> dict:
    token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY", "")
    if not token:
        raise RuntimeError(
            "NOTION_TOKEN or NOTION_API_KEY is not set. Please add it to your settings.json."
        )
    return {
        "NOTION_TOKEN": token,
        "NOTION_API_KEY": token,
    }