from pathlib import Path

SICILY_HOME = Path.home() / ".sicily"

async def get_google_config() -> dict:
    
    # Extract Client ID and Secret from your existing credentials file
    # (or hardcode them / put them in env vars if you prefer)
    credentials_path = SICILY_HOME / "google_credentials.json"

    if not credentials_path.exists():
        raise FileNotFoundError(
            f"❌ {credentials_path} not found. "
            "Download Desktop OAuth credentials from Google Cloud Console."
        )

    import json
    with open(credentials_path) as f:
        creds = json.load(f)

    # Handle both "installed" and "web" formats
    client_info = creds.get("installed") or creds.get("web") or creds
    client_id = client_info["client_id"]
    client_secret = client_info["client_secret"]

    return client_id, client_secret