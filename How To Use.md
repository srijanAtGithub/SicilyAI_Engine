# How to Use

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or pip

## Installation

```bash
uv tool install sicily
```

## First-time Setup

```bash
sicily init
```

This creates `~/.sicily/` and populates it with:

- `settings.json` — API keys and configuration
- `Souls/` — personality definition files (edit these to change how Sicily talks)
- `Context/` — long-term preferences, auto-managed by the agent
- `Recurring_Tasks/recurring_tasks.yaml` — scheduled task definitions

```bash
sicily config
```

Opens `~/.sicily/` in your file manager. Fill in `settings.json` with your API keys:

```json
{
    "OPENAI_API_KEY": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TAVILY_API_KEY": "",
    "GITHUB_TOKEN": "",
    "NOTION_TOKEN": "",
    "SPOTIFY_CLIENT_ID": "",
    "SPOTIFY_CLIENT_SECRET": "",
    "SPOTIFY_REDIRECT_URI": "",
    "TELEGRAM_API_ID": "",
    "TELEGRAM_API_HASH": "",
    "TELEGRAM_SESSION_STRING": ""
}
```

- All tokens are required to use the Agent mode:
  - `TELEGRAM_BOT_TOKEN` is the token of the telegram bot
  - Rest of the tokens are used for the respective connectors
- For Sicily Cowork and Sicily Navigator, only `OPENAI_API_KEY` is needed.

---

## Running Sicily

### Sicily Agent (Telegram)

```bash
sicily run
```

Starts the full agent: FastAPI backend, Telegram listener, session manager, and recurring task scheduler. Connect your Telegram bot and start chatting.

### Sicily Cowork (Local Terminal)

```bash
cd /path/to/your/project
sicily start
```

Locks the sandbox to your current directory, indexes all files, and drops you into an interactive terminal session. Ask anything about your files — Sicily will search the index first, then read only what it needs.

```
>>>: What were the key decisions in meeting notes?
>>>: What is the flight route for my Japan trip?
>>>: Find the document containing my Aadhar and PAN card
>>>: Summarise the Q3 report and compare it to Q2
>>>: Create a new file called summary.md with the main findings
```

Type `exit` or `quit` to end the session.

### Sicily Navigator (Browser Extension)

Navigator has two parts: a local backend server, and the Chrome extension itself.

**1. Start the backend:**

```bash
sicily navigator --start
```

This starts the Navigator server in the background on `http://127.0.0.1:8765`. Requires `OPENAI_API_KEY` (and `TAVILY_API_KEY` for research-backed features like "find more like this").

Manage the backend with:

```bash
sicily navigator --status   # check whether it's running
sicily navigator --stop     # stop it
```

**2. Load the extension in Chrome**, then use it in two ways:

- **Right-click any selected text** on any web page to get writing tools — rewrite, summarise, or ask a question about the selection.
- **Open the side panel** for the chat bot, one-click page summarise, one-click tab organiser, "find more like this," the reading list, drag-and-drop snippets and collections, and `@`-tab / `#`-collection references.

---

## CLI Reference

| Command               | Description                                                                                    |
| ---------------------- | ----------------------------------------------------------------------------------------------- |
| `sicily --version`     | Shows the installed version                                                                     |
| `sicily init`          | First-time setup — creates `~/.sicily/` with config templates                                   |
| `sicily config`        | Opens the config folder in your file manager                                                    |
| `sicily run`           | Starts the full Telegram agent (requires all API keys)                                          |
| `sicily start`         | Starts a local terminal session sandboxed to the current directory (requires only OpenAI key)   |
| `sicily navigator`     | Manages the Navigator backend for the browser extension — `--start`, `--stop`, `--status`       |
| `sicily usage`         | Shows token usage and estimated cost — `--session`, `--day`, `--week`                           |
| `sicily update`        | Updates Sicily to the latest published version                                                  |
| `sicily reset`         | Resets all config, Souls, Context, and file index back to defaults                              |
| `sicily uninstall`     | Deletes `~/.sicily/` and uninstalls the package                                                 |
| `sicily help`          | Lists available commands                                                                        |

---

## Customising Sicily

**Personality:** Edit `~/.sicily/Souls/*.md` to change how Sicily communicates. The Soul file is injected as part of the system prompt and can be swapped without touching any code.

**Scheduled tasks:** Edit `~/.sicily/Recurring_Tasks/recurring_tasks.yaml`. Set `enabled: false` to pause a task, or add new entries — no restart required on next run.

**Preferences:** Sicily builds these automatically over time. They live in `~/.sicily/Context/preferences.md` and can be edited manually if needed.