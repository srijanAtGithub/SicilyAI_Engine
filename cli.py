import click
import shutil
import json
import subprocess
import sys
import os
import signal
import time
from pathlib import Path

from configuration import REQUIRED_KEYS

try:
    from importlib.metadata import version as pkg_version
    __version__ = pkg_version("sicily")
except Exception:
    __version__ = "unknown"

# Folders managed by Sicily (reset and uninstall both use this list)
MANAGED_FOLDERS = ["Souls", "Context", "Recurring_Tasks", "file-index"]

main_cli = click.Group(name="sicily", help="Sicily — State-Locked Autonomous Agent")
SICILY_HOME = Path.home() / ".sicily"
SETTINGS_PATH = SICILY_HOME / "settings.json"


def ensure_initialized(required_keys=None):
    """Validates that init has been run and specified keys are configured."""
    if not SICILY_HOME.exists() or not SETTINGS_PATH.exists():
        click.secho("  Sicily is not initialized yet!", fg="yellow", bold=True)
        click.echo("Please run the initialization command first:")
        click.secho("  sicily init", fg="cyan")
        raise click.Abort()

    # Default to all keys if none specified
    if required_keys is None:
        required_keys = REQUIRED_KEYS

    try:
        with open(SETTINGS_PATH, "r") as f:
            settings = json.load(f)

        missing_or_placeholder = []
        for key in required_keys:
            val = settings.get(key, "")
            if not val or "your_" in val.lower() or "YOUR_" in val:
                missing_or_placeholder.append(key)

        if missing_or_placeholder:
            click.secho("  Configuration incomplete!", fg="yellow", bold=True)
            click.echo(f"The following keys in `{SETTINGS_PATH}` need to be configured:")
            for key in missing_or_placeholder:
                click.secho(f"  - {key}", fg="red")
            click.echo("\nPlease edit your settings file or run:")
            click.secho("  sicily config", fg="cyan")
            raise click.Abort()

    except json.JSONDecodeError:
        click.secho(" Error: `settings.json` is malformed or corrupted.", fg="red", bold=True)
        raise click.Abort()


@main_cli.command()
@click.version_option(version=__version__, prog_name="sicily")
def version():
    """Show the installed Sicily version."""
    pass  # --version flag is handled by @click.version_option


# Attach --version directly to the group so `sicily --version` works too
main_cli = click.Group(
    name="sicily",
    help="Sicily — State-Locked Autonomous Agent",
    params=[
        click.Option(
            ["--version"],
            is_flag=True,
            is_eager=True,
            expose_value=False,
            callback=lambda ctx, param, value: (
                click.echo(f"sicily {__version__}") or ctx.exit()
            ) if value else None,
            help="Show the version and exit.",
        )
    ],
)


@main_cli.command()
def init():
    """Initialize Sicily config, Souls, Context, and task files."""
    package_dir = Path(__file__).resolve().parent
    home = SICILY_HOME
    home.mkdir(exist_ok=True)

    src = package_dir / "settings.example.json"
    dest = home / "settings.json"
    if dest.exists():
        click.echo("~/.sicily/settings.json already exists, skipping.")
    else:
        shutil.copy(src, dest)
        click.echo("Created ~/.sicily/settings.json")

    for folder in ["Souls", "Context"]:
        if (home / folder).exists():
            click.echo(f"~/.sicily/{folder}/ already exists, skipping.")
        else:
            shutil.copytree(package_dir / folder, home / folder)
            click.echo(f"Created ~/.sicily/{folder}/")

    rt_dir = home / "Recurring_Tasks"
    yaml_dest = rt_dir / "recurring_tasks.yaml"
    if yaml_dest.exists():
        click.echo("~/.sicily/Recurring_Tasks/recurring_tasks.yaml already exists, skipping.")
    else:
        rt_dir.mkdir(exist_ok=True)
        shutil.copy(package_dir / "Recurring_Tasks" / "recurring_tasks.yaml", yaml_dest)
        click.echo("Created ~/.sicily/Recurring_Tasks/recurring_tasks.yaml")

    click.echo("\nNext steps:")
    click.echo("  1. Use `sicily config` to open the config folder")
    click.echo("  2. Edit Souls/*.md to define your agent's personality")
    click.echo("  3. Fill settings.json")
    click.echo("  4. sicily run")


@main_cli.command()
def config():
    """Open the ~/.sicily/ config folder in your file manager."""
    import subprocess
    import sys

    home = SICILY_HOME
    if not home.exists():
        click.echo("  ~/.sicily/ does not exist yet. Run `sicily init` first.")
        return

    click.echo(f" Opening {home} ...")
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(home)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", str(home)])
    else:
        subprocess.Popen(["xdg-open", str(home)])


@main_cli.command()
def run():
    """Start the Sicily agent."""
    ensure_initialized(required_keys=["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"])
    from Agent.main import main
    main()


@main_cli.command()
def start():
    """Start a local terminal session with sandboxed file access."""
    ensure_initialized(required_keys=["OPENAI_API_KEY"])
    import asyncio
    from Cowork.cowork_session import run_local_session
    asyncio.run(run_local_session())


NAVIGATOR_PID_FILE = SICILY_HOME / "navigator.pid"
NAVIGATOR_LOG_FILE = SICILY_HOME / "navigator.log"
NAVIGATOR_HOST = "127.0.0.1"
NAVIGATOR_PORT = 8765


def _navigator_pid_running(pid: int) -> bool:
    """Check whether a process with this PID is alive, cross-platform."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def _read_navigator_pid():
    if not NAVIGATOR_PID_FILE.exists():
        return None
    try:
        return int(NAVIGATOR_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _run_navigator_server():
    """Runs uvicorn in the foreground. Only called inside the detached child process."""
    import uvicorn
    uvicorn.run(
        "Navigator.navigator_bridge:app",
        host=NAVIGATOR_HOST,
        port=NAVIGATOR_PORT,
        log_level="info",
    )


def _get_downloads_dir() -> Path:
    """Resolve the system Downloads folder, cross-platform (Windows, macOS, Linux)."""
    home = Path.home()
    downloads = home / "Downloads"

    if sys.platform == "win32":
        # Downloads isn't always under %USERPROFILE% (e.g. OneDrive-redirected
        # folders) — ask the shell via the registry-backed known-folder API.
        try:
            import ctypes
            from ctypes import windll, wintypes

            FOLDERID_Downloads = "{374DE290-123F-4565-9164-39C4925E467B}"
            buf = ctypes.c_wchar_p()
            guid = ctypes.create_unicode_buffer(FOLDERID_Downloads)
            # SHGetKnownFolderPath needs a GUID struct, not a string; fall back
            # cleanly if anything about this isn't available.
            import uuid
            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8),
                ]
            rfid = GUID()
            u = uuid.UUID(FOLDERID_Downloads)
            rfid.Data1, rfid.Data2, rfid.Data3 = u.time_low, u.time_mid, u.time_hi_version
            rest = u.bytes[8:]
            for i, b in enumerate(rest):
                rfid.Data4[i] = b
            windll.shell32.SHGetKnownFolderPath(ctypes.byref(rfid), 0, 0, ctypes.byref(buf))
            if buf.value:
                downloads = Path(buf.value)
        except Exception:
            pass  # fall back to home / "Downloads" set above

    return downloads


def _find_navigator_extension_source() -> Path | None:
    """Locate the bundled Navigator/extension folder relative to the package."""
    package_dir = Path(__file__).resolve().parent
    candidates = [
        package_dir / "Navigator" / "extension",
        package_dir.parent / "Navigator" / "extension",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@main_cli.command(context_settings={"ignore_unknown_options": True})
@click.option("--start", "do_start", is_flag=True, help="Start the Navigator backend in the background.")
@click.option("--stop", "do_stop", is_flag=True, help="Stop the running Navigator backend.")
@click.option("--status", "do_status", is_flag=True, help="Check whether the Navigator backend is running.")
@click.option("--install", "do_install", is_flag=True, help="Copy the Navigator extension folder locally and show Chrome load-unpacked steps.")
@click.option("--foreground", "_foreground", is_flag=True, hidden=True,
              help="Internal: run the server in this process (used by --start's child process).")
def navigator(do_start, do_stop, do_status, do_install, _foreground):
    """Manage the Sicily Navigator backend and browser extension."""

    # Internal re-entry point: the detached background process calls itself
    # with this hidden flag so uvicorn actually runs somewhere.
    if _foreground:
        ensure_initialized(required_keys=["OPENAI_API_KEY"])
        _run_navigator_server()
        return

    flags_set = sum([do_start, do_stop, do_status, do_install])
    if flags_set == 0:
        click.secho("  Specify one of: --install, --start, --stop, --status", fg="yellow", bold=True)
        click.echo("  e.g.  sicily navigator --install")
        return
    if flags_set > 1:
        click.secho("  Please pass only one of --install / --start / --stop / --status at a time.", fg="red")
        raise click.Abort()

    if do_install:
        src = _find_navigator_extension_source()
        if src is None:
            click.secho("  ✗ Could not find the Navigator/extension folder in the package.", fg="red", bold=True)
            click.echo("  Your Sicily install may be corrupted — try `sicily update`.")
            raise click.Abort()

        downloads_dir = _get_downloads_dir()
        try:
            downloads_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads_dir = Path.home() / "Downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)

        dest = downloads_dir / "sicily-navigator-extension"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)

        click.secho("\n  ✓ Navigator extension copied to:", fg="green", bold=True)
        click.secho(f"    {dest}", fg="cyan")

        click.echo("\n  Now load it into Chrome:")
        click.echo("    1. Open Chrome and go to  chrome://extensions")
        click.echo("    2. Turn on 'Developer mode' (top-right toggle)")
        click.echo("    3. Click 'Load unpacked'")
        click.echo(f"    4. Select this folder:  {dest}")
        click.echo("    5. The Sicily Navigator icon should now appear in your Chrome toolbar")

        click.echo("\n  Then start the backend if it isn't already running:")
        click.secho("    sicily navigator --start\n", fg="cyan")

        click.secho("  Note:", fg="yellow", bold=True)
        click.echo("  If you had tabs open BEFORE running `sicily navigator --start`,")
        click.echo("  reload those tabs — otherwise the right-click writing tools")
        click.echo("  (the floating context-menu window) won't appear on them.\n")
        return

    if do_status:
        pid = _read_navigator_pid()
        if pid and _navigator_pid_running(pid):
            click.secho(f"  ● Navigator is running (pid {pid}) on http://{NAVIGATOR_HOST}:{NAVIGATOR_PORT}", fg="green", bold=True)
        else:
            click.secho("  ○ Navigator is not running", fg="yellow")
        return

    if do_stop:
        pid = _read_navigator_pid()
        if not pid or not _navigator_pid_running(pid):
            click.echo("  Navigator is not running.")
            if NAVIGATOR_PID_FILE.exists():
                NAVIGATOR_PID_FILE.unlink()
            return
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
            click.secho(f"  ✓ Stopped Navigator (pid {pid})", fg="green", bold=True)
        except Exception as e:
            click.secho(f"  ✗ Could not stop Navigator: {e}", fg="red")
        finally:
            if NAVIGATOR_PID_FILE.exists():
                NAVIGATOR_PID_FILE.unlink()
        return

    if do_start:
        ensure_initialized(required_keys=["OPENAI_API_KEY", "TAVILY_API_KEY"])

        existing_pid = _read_navigator_pid()
        if existing_pid and _navigator_pid_running(existing_pid):
            click.secho(f"  Navigator is already running (pid {existing_pid}).", fg="yellow")
            click.echo(f"  http://{NAVIGATOR_HOST}:{NAVIGATOR_PORT}")
            return

        SICILY_HOME.mkdir(exist_ok=True)
        log_fh = open(NAVIGATOR_LOG_FILE, "a")

        # Re-invoke this same CLI entry point with the hidden --foreground flag,
        # fully detached so it survives the parent terminal closing.
        # Uses the installed 'sicily' console-script so this works regardless
        # of whether the user installed via `uv tool install` or pip.
        sicily_bin = shutil.which("sicily") or sys.argv[0]
        cmd = [sicily_bin, "navigator", "--foreground"]

        popen_kwargs = dict(stdout=log_fh, stderr=log_fh, stdin=subprocess.DEVNULL)
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kwargs["start_new_session"] = True  # detach from controlling terminal

        proc = subprocess.Popen(cmd, **popen_kwargs)
        NAVIGATOR_PID_FILE.write_text(str(proc.pid))

        # Brief liveness check so we can report a clear failure instead of a false "started".
        time.sleep(0.75)
        if not _navigator_pid_running(proc.pid):
            click.secho("  ✗ Navigator failed to start. Check the log:", fg="red", bold=True)
            click.echo(f"    {NAVIGATOR_LOG_FILE}")
            if NAVIGATOR_PID_FILE.exists():
                NAVIGATOR_PID_FILE.unlink()
            raise click.Abort()

        click.secho(f"\n  ✓ Navigator started in background (pid {proc.pid})", fg="green", bold=True)
        click.echo(f"  http://{NAVIGATOR_HOST}:{NAVIGATOR_PORT}")
        click.echo(f"  Logs: {NAVIGATOR_LOG_FILE}")
        click.echo("  Check status:  sicily navigator --status")
        click.echo("  Stop:          sicily navigator --stop\n")


@main_cli.command(name="reset")
def reset():
    """Reset all config, personalization, and indexed files back to default."""
    if not SICILY_HOME.exists():
        click.secho("  Nothing to reset — ~/.sicily/ does not exist.", fg="yellow")
        return

    click.secho("\n  This will permanently reset:", fg="yellow", bold=True)
    click.echo("  • settings.json          (wiped and restored from template)")
    click.echo("  • Souls/                 (all personality files reset)")
    click.echo("  • Context/               (all context files reset)")
    click.echo("  • Recurring_Tasks/       (task schedule reset)")
    click.echo("  • file-index/            (ChromaDB, TF-IDF index, registry — deleted)")
    click.echo()

    if not click.confirm("  Are you sure you want to reset everything?", default=False):
        click.echo("Aborted.")
        return

    package_dir = Path(__file__).resolve().parent
    home = SICILY_HOME
    errors = []

    # Reset settings.json
    src = package_dir / "settings.example.json"
    dest = home / "settings.json"
    try:
        if src.exists():
            shutil.copy(src, dest)
            click.echo("  ✓ Reset settings.json")
        else:
            click.secho("  ✗ settings.example.json not found in package — skipping.", fg="yellow")
    except Exception as e:
        errors.append(f"settings.json: {e}")

    # Reset Souls/ and Context/
    for folder in ["Souls", "Context"]:
        target = home / folder
        src_folder = package_dir / folder
        try:
            if target.exists():
                shutil.rmtree(target)
            if src_folder.exists():
                shutil.copytree(src_folder, target)
                click.echo(f"  ✓ Reset {folder}/")
            else:
                click.secho(f"  ✗ {folder}/ not found in package — skipping.", fg="yellow")
        except Exception as e:
            errors.append(f"{folder}/: {e}")

    # Reset Recurring_Tasks/
    rt_dir = home / "Recurring_Tasks"
    yaml_src = package_dir / "Recurring_Tasks" / "recurring_tasks.yaml"
    yaml_dest = rt_dir / "recurring_tasks.yaml"
    try:
        rt_dir.mkdir(exist_ok=True)
        if yaml_src.exists():
            shutil.copy(yaml_src, yaml_dest)
            click.echo("  ✓ Reset Recurring_Tasks/recurring_tasks.yaml")
        else:
            click.secho("  ✗ recurring_tasks.yaml not found in package — skipping.", fg="yellow")
    except Exception as e:
        errors.append(f"Recurring_Tasks/: {e}")

    # Delete file-index/ entirely
    file_index = home / "file-index"
    try:
        if file_index.exists():
            shutil.rmtree(file_index)
            click.echo("  ✓ Deleted file-index/ (ChromaDB, TF-IDF, registry)")
        else:
            click.echo("  ✓ file-index/ did not exist — nothing to remove")
    except Exception as e:
        errors.append(f"file-index/: {e}")

    if errors:
        click.secho("\n  Some items could not be reset:", fg="red", bold=True)
        for err in errors:
            click.secho(f"  ✗ {err}", fg="red")
    else:
        click.secho("\n  Reset complete.", fg="green", bold=True)
        click.echo("Run `sicily init` to re-initialize, then fill in your settings.")


@main_cli.command()
def update():
    """Update Sicily to the latest published version."""
    uv_path = shutil.which("uv")
    if not uv_path:
        click.secho("  uv not found.", fg="red", bold=True)
        click.echo("Sicily is managed via uv. Install it from https://docs.astral.sh/uv/")
        click.secho("  Then run:  uv tool install --reinstall sicily", fg="cyan")
        return

    click.echo("  Checking for updates...")

    if sys.platform == "win32":
        # On Windows, `sicily.exe` cannot overwrite itself while it's the
        # running process — the file is locked by the OS. Detach `uv` into
        # an independent process and exit immediately so the lock is
        # released before uv tries to replace the launcher.
        click.echo("  Relaunching the update in the background so sicily.exe can be replaced...")
        log_path = SICILY_HOME / "update.log"
        SICILY_HOME.mkdir(exist_ok=True)
        log_fh = open(log_path, "a")
        subprocess.Popen(
            [uv_path, "tool", "install", "--reinstall", "sicily"],
            stdout=log_fh, stderr=log_fh, stdin=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        click.secho("  ✓ Update started in the background.", fg="green", bold=True)
        click.echo(f"  Check progress / errors in: {log_path}")
        click.echo("  Run `sicily --version` in a new terminal in a few seconds to confirm.")
        return

    result = subprocess.run(
        [uv_path, "tool", "install", "--reinstall", "sicily"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        click.secho("  ✓ Sicily updated successfully.", fg="green")
    else:
        click.secho("  ✗ Update failed. Try running manually:", fg="red")
        click.secho("      uv tool install --reinstall sicily", fg="cyan")
        if result.stdout:
            click.echo(f"  uv output: {result.stdout.strip()}")
        if result.stderr:
            click.echo(f"  uv error: {result.stderr.strip()}")


@main_cli.command()
def uninstall():
    """Remove all local Sicily files and uninstall the package."""
    click.secho("\n  This will permanently:", fg="yellow", bold=True)
    click.echo("  • Delete ~/.sicily/  (all config, Souls, Context, indexes — everything)")
    click.echo("  • Run:  pip uninstall sicily -y")
    click.echo()

    if not click.confirm("  Are you sure you want to uninstall Sicily?", default=False):
        click.echo("Aborted.")
        return

    # Remove ~/.sicily/
    if SICILY_HOME.exists():
        try:
            shutil.rmtree(SICILY_HOME)
            click.secho("  ✓ Deleted ~/.sicily/", fg="green")
        except Exception as e:
            click.secho(f"  ✗ Could not delete ~/.sicily/: {e}", fg="red")
    else:
        click.echo("  ✓ ~/.sicily/ did not exist — nothing to remove")

    # Uninstall the package — prefer uv (the recommended install method),
    # fall back to pip for anyone who installed via pip directly.
    import subprocess
    import sys

    uv_path = shutil.which("uv")
    if uv_path:
        click.echo("  Uninstalling sicily via uv...")
        result = subprocess.run(
            [uv_path, "tool", "uninstall", "sicily"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            click.secho("  ✓ sicily uninstalled successfully.", fg="green")
        else:
            click.secho("  ✗ uv tool uninstall failed. Try running manually:", fg="red")
            click.secho("      uv tool uninstall sicily", fg="cyan")
            if result.stderr:
                click.echo(f"  uv error: {result.stderr.strip()}")
    else:
        click.echo("  uv not found — falling back to pip...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "sicily", "-y"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            click.secho("  ✓ sicily uninstalled successfully.", fg="green")
        else:
            click.secho("  ✗ pip uninstall failed. Try running manually:", fg="red")
            click.secho("      pip uninstall sicily -y", fg="cyan")
            if result.stderr:
                click.echo(f"  pip error: {result.stderr.strip()}")


@main_cli.command(name="usage")
@click.option("--session", is_flag=True, help="Show usage for the last recorded session.")
@click.option("--day", is_flag=True, help="Show usage for the last 24 hours.")
@click.option("--week", is_flag=True, help="Show usage for the last 7 days (default).")
def usage(session, day, week):
    """Show token usage and estimated cost."""
    from usage_tracker import get_usage_report, init_db, cleanup_old_records
    from rich.console import Console
    from rich.table import Table

    init_db()
    cleanup_old_records()

    if session:
        timeframe = "session"
        title_suffix = "Last Session"
    elif day:
        timeframe = "day"
        title_suffix = "Last 24 Hours"
    else:
        timeframe = "week"
        title_suffix = "Last 7 Days"

    report = get_usage_report(timeframe=timeframe)
    console = Console()
    
    if not report:
        console.print(f"\n[yellow]No usage data found for: {title_suffix}[/yellow]\n")
        return

    table = Table(title=f"Sicily Usage Report ({title_suffix})")
    table.add_column("Dimension", style="cyan")
    table.add_column("Model", style="magenta")
    table.add_column("Input Tokens", justify="right", style="green")
    table.add_column("Output Tokens", justify="right", style="green")
    table.add_column("Est. Cost (USD)", justify="right", style="bold yellow")

    total_cost = 0.0
    for row in report:
        table.add_row(
            row["dimension"].capitalize(),
            row["model_name"],
            f"{row['in_tokens']:,}",
            f"{row['out_tokens']:,}",
            f"${row['total_cost']:.5f}"
        )
        total_cost += row['total_cost']

    console.print()
    console.print(table)
    console.print(f"[bold right]Total Estimated Cost: ${total_cost:.5f}[/bold right]\n")


@main_cli.command()
def help():
    """Show help."""
    click.echo("\nAvailable commands:")
    click.echo("  --version     - Show the installed version")
    click.echo("  init          - Initialize the project")
    click.echo("  config        - Open the config folder")
    click.echo("  run           - Run the agent")
    click.echo("  start         - Start a local terminal session")
    click.echo("  navigator     - Manage the browser extension backend")
    click.echo("                    --install / --start / --stop / --status")
    click.echo("  usage         - Show token usage and estimated cost")
    click.echo("  update        - Update Sicily to the latest version")
    click.echo("  reset         - Reset all config and indexes back to default")
    click.echo("  uninstall     - Remove all local files and uninstall sicily")