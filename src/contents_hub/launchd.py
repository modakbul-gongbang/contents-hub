"""
macOS launchd integration for contents-hub daemon.

Provides plist generation, install/uninstall, and status checking
for the background daemon process.

Usage via CLI:
    python -m contents_hub daemon install
    python -m contents_hub daemon uninstall
    python -m contents_hub daemon status
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from contents_hub.config import WikiConfig
from contents_hub.naming import LAUNCHD_LABEL, PRODUCT_NAME, VAULT_ENV_VARS

LABEL = LAUNCHD_LABEL.canonical
PLIST_FILENAME = f"{LABEL}.plist"
LOG_DIR = Path(f"~/.{PRODUCT_NAME.canonical}")


def _log_dir() -> Path:
    """Return the user-level launchd log directory."""
    return LOG_DIR.expanduser()


def _plist_path(label: str = LABEL) -> Path:
    """Return the path to the launchd plist file."""
    return Path("~/Library/LaunchAgents").expanduser() / f"{label}.plist"


def _unload_plist(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", "unload", str(path)],
        capture_output=True,
        text=True,
    )


def _detect_python() -> str:
    """Detect the Python executable path.

    Prefers the current interpreter. Falls back to a .venv in the
    plugin root if the current interpreter is inside one.
    """
    return sys.executable


def _plugin_root() -> str:
    """Detect the plugin root directory.

    Uses the parent of the contents_hub package directory.
    """
    import contents_hub

    pkg_dir = Path(contents_hub.__file__).resolve().parent
    # Go up from src/contents_hub/ to the project root.
    # If installed as editable, pkg_dir is src/contents_hub.
    # We want the project root (parent of src/)
    candidate = pkg_dir.parent.parent
    if (candidate / "pyproject.toml").exists():
        return str(candidate)
    # Fallback: use the package directory itself
    return str(pkg_dir.parent)


def generate_plist(config: WikiConfig) -> str:
    """Generate macOS launchd plist XML for the daemon.

    Args:
        config: Wiki configuration with vault_path.

    Returns:
        Plist XML string.
    """
    python_path = _detect_python()
    vault_path = str(config.vault_path)
    plugin_root = _plugin_root()

    log_dir = _log_dir()
    stdout_log = str(log_dir / "daemon.log")
    stderr_log = str(log_dir / "daemon-error.log")

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>-m</string>
        <string>contents_hub.daemon</string>
        <string>--vault</string>
        <string>{vault_path}</string>
        <string>loop</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{stdout_log}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_log}</string>
    <key>WorkingDirectory</key>
    <string>{plugin_root}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>{VAULT_ENV_VARS.canonical}</key>
        <string>{vault_path}</string>
    </dict>
</dict>
</plist>
"""
    return plist


def install(config: WikiConfig) -> str:
    """Install the daemon via launchd.

    1. Generate plist XML
    2. Ensure log directory exists
    3. Write plist to ~/Library/LaunchAgents/
    4. Load the plist via launchctl

    Args:
        config: Wiki configuration.

    Returns:
        Status message string.
    """
    plist_path = _plist_path()

    # Ensure directories exist
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    if plist_path.exists():
        _unload_plist(plist_path)

    # Generate and write plist
    plist_content = generate_plist(config)
    plist_path.write_text(plist_content, encoding="utf-8")

    # Load the plist
    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        return f"Plist written to {plist_path} but launchctl load failed: {error}"

    return f"Daemon installed and loaded.\n  plist: {plist_path}\n  logs:  {log_dir}/daemon.log"


def uninstall() -> str:
    """Uninstall the daemon.

    1. Unload via launchctl
    2. Remove the plist file

    Returns:
        Status message string.
    """
    messages: list[str] = []
    had_plist = False

    plist_path = _plist_path()
    if plist_path.exists():
        had_plist = True
        result = _unload_plist(plist_path)
        plist_path.unlink(missing_ok=True)
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            messages.append(
                f"Plist removed from {plist_path} but launchctl unload had warnings: {error}"
            )
        else:
            messages.append(f"Plist removed from {plist_path}")

    if not had_plist:
        return f"Plist not found at {_plist_path()}. Daemon is not installed."

    return "Daemon uninstalled. " + "; ".join(messages)


def status() -> str:
    """Check if the daemon is loaded via launchctl.

    Returns:
        Status message string.
    """
    result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return "Could not query launchctl."

    loaded: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        if LABEL not in line:
            continue
        parts = line.split()
        # launchctl list format: PID  Status  Label
        pid = parts[0] if len(parts) >= 3 else "-"
        exit_status = parts[1] if len(parts) >= 3 else "-"
        loaded[LABEL] = (pid, exit_status)

    if LABEL in loaded:
        pid, exit_status = loaded[LABEL]
        if pid == "-":
            return f"Daemon is loaded but NOT running (exit status: {exit_status})"
        return f"Daemon is running (PID: {pid})"

    # Check if plist exists but not loaded
    plist_path = _plist_path()
    if plist_path.exists():
        return "Daemon plist exists but is NOT loaded. Run 'daemon install' to load."

    return "Daemon is not installed."
