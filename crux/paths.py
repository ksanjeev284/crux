"""Where the desktop app stores wallet, settings and inbox.

From source, that is the working directory (the clone). Inside a frozen
executable it is a per-user data directory, because the binary itself may
live somewhere read-only (Program Files, a .app bundle, /usr/local).
"""

from __future__ import annotations

import os
import sys

APP_NAME = "CRUX"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def user_data_dir() -> str:
    if not frozen():
        return os.getcwd()
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(root, APP_NAME)
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/CRUX")
    root = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(root, "crux")


def default_runtime_paths() -> dict:
    home = user_data_dir()
    chain = os.path.join(home, "chain")
    return {
        "home": home,
        "wallet_path": os.path.join(home, "crux-wallet.json"),
        "settings_path": os.path.join(home, "crux-gui.json"),
        "inbox_dir": os.path.join(home, "inbox"),
        "blocks_path": os.path.join(chain, "blocks.jsonl"),
        "registry_path": os.path.join(chain, "registry.json"),
        "mempool_path": os.path.join(chain, "mempool.jsonl"),
    }


def resolve_wallet_path(preferred: str) -> str:
    """
    Use `preferred` if it exists. Fall back to cwd / the frozen executable
    only when `preferred` is the default data-dir wallet, so a test that
    points at a temp path cannot pick up a different wallet by accident.
    """
    preferred = os.path.abspath(preferred)
    if os.path.isfile(preferred):
        return preferred
    default_home = os.path.abspath(default_runtime_paths()["wallet_path"])
    cwd_wallet = os.path.abspath(os.path.join(os.getcwd(), "crux-wallet.json"))
    if preferred not in {default_home, cwd_wallet}:
        return preferred
    candidates = [cwd_wallet, default_home]
    if frozen():
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, "crux-wallet.json"))
    seen = set()
    for path in candidates:
        abs_path = os.path.abspath(path)
        if abs_path in seen:
            continue
        seen.add(abs_path)
        if os.path.isfile(abs_path):
            return abs_path
    return preferred
