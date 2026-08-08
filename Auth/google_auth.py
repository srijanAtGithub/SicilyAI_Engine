from pathlib import Path
import json
import structlog

log = structlog.get_logger()

SICILY_HOME = Path.home() / ".sicily"


async def get_google_config() -> tuple[str, str]:
    """
    Extract Client ID and Secret from the credentials file.
    Returns (client_id, client_secret).
    """
    credentials_path = SICILY_HOME / "google_credentials.json"

    if not credentials_path.exists():
        raise FileNotFoundError(
            f"❌ {credentials_path} not found. "
            "Download Desktop OAuth credentials from Google Cloud Console."
        )

    with open(credentials_path) as f:
        creds = json.load(f)

    # Handle both "installed" and "web" formats
    client_info = creds.get("installed") or creds.get("web") or creds
    client_id = client_info["client_id"]
    client_secret = client_info["client_secret"]

    return client_id, client_secret


async def auto_auth(tools):
    """
    If no Google Workspace account is configured, trigger the
    manage_accounts authenticate flow.
    """
    try:
        manage_accounts = next(
            (t for t in tools if t.name == "manage_accounts"),
            None
        )

        if manage_accounts is None:
            log.warning("manage_accounts tool not found — skipping auto-auth check")
            return

        # 1. Check existing accounts
        list_result = await manage_accounts.ainvoke({"operation": "list"})
        list_text = str(list_result).lower()

        # Heuristic: if the result mentions no accounts / empty / not configured
        no_accounts = any(
            phrase in list_text
            for phrase in [
                "no accounts",
                "no account",
                "not configured",
                "empty",
                "none",
                "[]",
            ]
        ) or "email" not in list_text

        if no_accounts:
            log.info("No Google Workspace account found — starting authentication flow")
            print("\n🔐 No Google account configured yet.")
            print("Opening browser for Google login...\n")

            # 2. Trigger authentication (opens browser)
            await manage_accounts.ainvoke({"operation": "authenticate"})
            log.info("Google Workspace authentication flow completed (or opened)")
        else:
            log.info("Google Workspace account(s) already configured")

    except Exception as e:
        # Never crash the whole connector load because of auth helper
        log.error("auto_auth_check_failed", error=str(e))
