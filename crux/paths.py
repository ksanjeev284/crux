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
