#!/usr/bin/env python3
"""
CRUX desktop GUI. Standard library only — tkinter, no pip install.

    python3 gui.py
    python3 gui.py --wallet crux-wallet.json

Four tabs: the chain, the wallet, the miner, and an independent verifier.
Submissions are the same `crux-*-v1:` lines the CLI writes; the GUI just
makes them copyable and opens the GitHub issue form in a browser.
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from tkinter import filedialog, messagebox, ttk

from crux import __version__
from crux.consensus import format_amount
from crux.paths import default_runtime_paths, frozen, resolve_wallet_path, user_data_dir
from crux.desktop import (
    DEFAULT_FEE,
    DEFAULT_REPO,
    EXPLORER_URL,
    SETTINGS_FILE,
    WALLET_FILE,
    DesktopError,
    autofill_from_wallet,
    build_identity,
    build_send,
    create_wallet,
    fetch_remote,
    issue_url,
    load_mempool,
    load_registry,
    load_settings,
    load_wallet,
    mine_block,
    save_settings,
    short_hash,
    snapshot,
    submit_block,
    validate_miner_fields,
    verify_chain,
    wallet_present,
)
from crux import chain as chainmod

BG = "#0E0C0A"
PANEL = "#161310"
PANEL2 = "#1C1814"
LINE = "#2E2820"
LINE2 = "#3A332B"
INK = "#EBE6DE"
DIM = "#8A8276"
DIM2 = "#5F574C"
COPPER = "#E08A45"
COPPER_DIM = "#9A4A18"
TEAL = "#5CA3B0"
GREEN = "#5FB07A"
RED = "#D9614C"


def _pick_font(root) -> str:
    fams = set(tkfont.families(root))
    for name in ("Cascadia Mono", "Consolas", "Menlo", "SF Mono", "Courier New"):
        if name in fams:
            return name
    return "Courier"


class App:
    """Tk front-end. All chain/wallet work goes through crux.desktop."""

    def __init__(
        self,
        root: tk.Tk,
        *,
        wallet_path: str = WALLET_FILE,
        settings_path: str = SETTINGS_FILE,
        blocks_path: str | None = None,
        registry_path: str | None = None,
        mempool_path: str | None = None,
        inbox_dir: str = "inbox",
        repo: str | None = None,
    ):
        self.root = root
        self.settings_path = settings_path
        self.blocks_path = blocks_path or chainmod.BLOCKS_FILE
        self.registry_path = registry_path or os.path.join("chain", "registry.json")
        self.mempool_path = mempool_path or os.path.join("chain", "mempool.jsonl")
        self.inbox_dir = inbox_dir

        loaded = load_settings(settings_path)
        saved_wallet = (loaded.get("wallet_path") or "").strip()
        if saved_wallet and os.path.isfile(saved_wallet) and not os.path.isfile(wallet_path):
            wallet_path = saved_wallet
        self.wallet_path = resolve_wallet_path(wallet_path)

        self.settings = load_settings(settings_path)
        if repo:
            self.settings["repo"] = repo
        if wallet_present(self.wallet_path):
            try:
                early = load_wallet(self.wallet_path)
            except DesktopError:
                early = None
            if early:
                self.settings = autofill_from_wallet(self.settings, early)

        self.queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.mining = False
        self.blocks = []
        self.mempool = []
        self.registry = {}
        self.snap: dict = {}
        self.last_result: dict | None = None
        self._pumping = True
        self._ready = False
        self._save_after = None
        self.dialogs = messagebox

        self._style()
        self._vars()
        self._build()
        self._restore_window()
        self.refresh()
        self._restore_last_result()
        try:
            self.select_tab(self.settings.get("last_tab") or "Chain")
        except KeyError:
            pass
        self._watch_fields()
        self._ready = True
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_after = self.root.after(200, self._pump)

    # ------------------------------------------------------------------ ui
    def _style(self):
        self.root.title(f"CRUX {__version__}")
        self.root.configure(bg=BG)
        self.root.minsize(860, 580)
        self.root.geometry("1020x700")
        try:
            self.root.tk.call("tk", "scaling", 1.15)
        except tk.TclError:
            pass

        self.font = _pick_font(self.root)
        self.fn = (self.font, 10)
        self.fn_sm = (self.font, 9)
        self.fn_lg = (self.font, 13, "bold")
        self.fn_brand = (self.font, 16, "bold")

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=INK, fieldbackground=PANEL2,
                        bordercolor=LINE, troughcolor=PANEL, font=self.fn)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=INK, font=self.fn)
        style.configure("Dim.TLabel", background=BG, foreground=DIM, font=self.fn_sm)
        style.configure("Brand.TLabel", background=BG, foreground=COPPER, font=self.fn_brand)
        style.configure("StatCap.TLabel", background=PANEL, foreground=DIM2,
                        font=(self.font, 8))
        style.configure("StatVal.TLabel", background=PANEL, foreground=INK, font=self.fn_lg)
        style.configure("StatSub.TLabel", background=PANEL, foreground=DIM, font=self.fn_sm)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL2, foreground=DIM,
                        padding=(14, 6), font=self.fn)
        style.map("TNotebook.Tab",
                  background=[("selected", PANEL)],
                  foreground=[("selected", COPPER)])
        style.configure("TEntry", fieldbackground=PANEL2, foreground=INK,
                        insertcolor=INK, bordercolor=LINE2, padding=4)
        style.configure("TCheckbutton", background=BG, foreground=INK, font=self.fn)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=INK, rowheight=24, bordercolor=LINE, font=self.fn_sm)
        style.configure("Treeview.Heading", background=PANEL2, foreground=DIM2,
                        font=(self.font, 8), relief="flat")
        style.map("Treeview",
                  background=[("selected", COPPER_DIM)],
                  foreground=[("selected", INK)])
        style.configure("TScrollbar", background=PANEL2, troughcolor=PANEL,
                        bordercolor=LINE, arrowcolor=DIM)
        style.configure("TButton", background=PANEL2, foreground=INK,
                        bordercolor=LINE2, padding=(10, 5), font=self.fn)
        style.map("TButton",
                  background=[("active", LINE2), ("disabled", PANEL)],
                  foreground=[("disabled", DIM2)])
        style.configure("Copper.TButton", background=COPPER_DIM, foreground=INK,
                        bordercolor=COPPER, padding=(12, 6), font=self.fn)
        style.map("Copper.TButton", background=[("active", COPPER)])

    def _vars(self):
        self.height_var = tk.StringVar(value="—")
        self.diff_var = tk.StringVar(value="—")
        self.supply_var = tk.StringVar(value="—")
        self.tip_var = tk.StringVar(value="—")
        self.height_sub = tk.StringVar(value="blocks")
        self.diff_sub = tk.StringVar(value="bits")
        self.supply_sub = tk.StringVar(value="unspent")
        self.tip_sub = tk.StringVar(value="hash")
        self.status_var = tk.StringVar(value="starting…")
        self.address_var = tk.StringVar(value="no wallet")
        self.balance_var = tk.StringVar(value="—")
        self.mature_var = tk.StringVar(value="")
        self.handle_var = tk.StringVar(value=self.settings.get("handle", ""))
        self.message_var = tk.StringVar(value=self.settings.get("message", "gm"))
        self.repo_var = tk.StringVar(value=self.settings.get("repo", DEFAULT_REPO))
        self.source_var = tk.StringVar(value=self.settings.get("source", "local"))
        self.submit_var = tk.BooleanVar(value=bool(self.settings.get("submit")))
        self.keep_mining_var = tk.BooleanVar(value=bool(self.settings.get("keep_mining")))
        self.to_var = tk.StringVar(value=self.settings.get("to", ""))
        self.amount_var = tk.StringVar(value=self.settings.get("amount", ""))
        self.fee_var = tk.StringVar(value=self.settings.get("fee", DEFAULT_FEE))
        self.memo_var = tk.StringVar(value=self.settings.get("memo", ""))
        self.id_handle_var = tk.StringVar(
            value=self.settings.get("id_handle") or self.settings.get("handle", "")
        )
        self.search_var = tk.StringVar()
        self.payout_var = tk.StringVar(value=self.settings.get("payout", ""))
        self.pubkey_var = tk.StringVar(value="")
        self.wallet_path_var = tk.StringVar(value=self.wallet_path)

    def _build(self):
        pad = {"padx": 12, "pady": 8}
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", **pad)
        ttk.Label(header, text="CRUX", style="Brand.TLabel").pack(side="left")
        ttk.Label(header, text=f"desktop  v{__version__}", style="Dim.TLabel").pack(
            side="left", padx=(8, 0))

        ttk.Button(header, text="Data folder", command=self.open_data_dir).pack(side="right")
        ttk.Button(header, text="Explorer", command=self.open_explorer).pack(
            side="right", padx=(0, 8))
        ttk.Button(header, text="Refresh", command=self.refresh).pack(side="right", padx=(0, 8))
        tk.Entry(header, textvariable=self.repo_var, bg=PANEL2, fg=INK, insertbackground=INK,
                 relief="flat", font=self.fn, width=28,
                 highlightthickness=1, highlightbackground=LINE2,
                 highlightcolor=COPPER).pack(side="right", padx=(0, 8), ipady=3)

        stats = tk.Frame(self.root, bg=LINE, highlightthickness=0)
        stats.pack(fill="x", padx=12)
        self._stat(stats, "HEIGHT", self.height_var, self.height_sub).pack(
            side="left", fill="both", expand=True, padx=(0, 1))
        self._stat(stats, "DIFFICULTY", self.diff_var, self.diff_sub).pack(
            side="left", fill="both", expand=True, padx=(0, 1))
        self._stat(stats, "SUPPLY", self.supply_var, self.supply_sub).pack(
            side="left", fill="both", expand=True, padx=(0, 1))
        self._stat(stats, "TIP", self.tip_var, self.tip_sub).pack(
            side="left", fill="both", expand=True)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(10, 0))

        self.tab_chain = tk.Frame(self.notebook, bg=BG)
        self.tab_wallet = tk.Frame(self.notebook, bg=BG)
        self.tab_mine = tk.Frame(self.notebook, bg=BG)
        self.tab_verify = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.tab_chain, text="Chain")
        self.notebook.add(self.tab_wallet, text="Wallet")
        self.notebook.add(self.tab_mine, text="Mine")
        self.notebook.add(self.tab_verify, text="Verify")

        self._build_chain(self.tab_chain)
        self._build_wallet(self.tab_wallet)
        self._build_mine(self.tab_mine)
        self._build_verify(self.tab_verify)

        status = tk.Frame(self.root, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        status.pack(fill="x", padx=12, pady=10)
        ttk.Label(status, textvariable=self.status_var, style="Dim.TLabel",
                  background=PANEL).pack(side="left", padx=10, pady=5)

    def _stat(self, parent, caption, value_var, sub_var):
        box = tk.Frame(parent, bg=PANEL)
        tk.Label(box, text=caption, bg=PANEL, fg=DIM2, font=(self.font, 8),
                 anchor="w").pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(box, textvariable=value_var, bg=PANEL, fg=INK, font=self.fn_lg,
                 anchor="w").pack(fill="x", padx=12)
        tk.Label(box, textvariable=sub_var, bg=PANEL, fg=DIM, font=self.fn_sm,
                 anchor="w").pack(fill="x", padx=12, pady=(0, 8))
        return box

    def _entry(self, parent, var, width=40):
        e = tk.Entry(parent, textvariable=var, bg=PANEL2, fg=INK, insertbackground=INK,
                     relief="flat", font=self.fn, width=width,
                     highlightthickness=1, highlightbackground=LINE2, highlightcolor=COPPER)
        return e

    def _label_row(self, parent, text, var, width=48):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=text, bg=BG, fg=DIM, font=self.fn, width=12,
                 anchor="w").pack(side="left")
        self._entry(row, var, width=width).pack(side="left", fill="x", expand=True, ipady=4)
        return row

    def _tree(self, parent, columns, headings, heights=8):
        wrap = tk.Frame(parent, bg=LINE)
        wrap.pack(fill="both", expand=True, pady=(4, 8))
        tree = ttk.Treeview(wrap, columns=columns, show="headings", height=heights)
        for col, head, w, anchor in headings:
            tree.heading(col, text=head)
            tree.column(col, width=w, anchor=anchor, stretch=True)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        return tree

    def _build_chain(self, tab):
        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", pady=(8, 4))
        tk.Label(bar, text="Search", bg=BG, fg=DIM, font=self.fn).pack(side="left")
        search = self._entry(bar, self.search_var, width=50)
        search.pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        search.bind("<Return>", lambda _e: self.do_search())
        ttk.Button(bar, text="Go", command=self.do_search).pack(side="left")

        body = tk.Frame(tab, bg=BG)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = tk.Frame(body, bg=BG)
        right.pack(side="right", fill="both", expand=True)

        tk.Label(left, text="BLOCKS", bg=BG, fg=DIM, font=(self.font, 8)).pack(anchor="w")
        self.blocks_tree = self._tree(
            left, ("h", "miner", "msg", "txs", "hash"),
            (("h", "#", 50, "e"), ("miner", "miner", 110, "w"),
             ("msg", "message", 220, "w"), ("txs", "txs", 40, "e"),
             ("hash", "hash", 140, "w")),
            heights=9,
        )
        self.blocks_tree.bind("<<TreeviewSelect>>", self._on_block_select)

        tk.Label(left, text="MEMPOOL", bg=BG, fg=DIM, font=(self.font, 8)).pack(anchor="w")
        self.mempool_tree = self._tree(
            left, ("txid", "to", "amt"),
            (("txid", "txid", 140, "w"), ("to", "to", 180, "w"),
             ("amt", "amount", 100, "e")),
            heights=4,
        )

        tk.Label(right, text="MINERS", bg=BG, fg=DIM, font=(self.font, 8)).pack(anchor="w")
        self.miners_tree = self._tree(
            right, ("miner", "blocks", "share"),
            (("miner", "miner", 140, "w"), ("blocks", "blocks", 70, "e"),
             ("share", "share", 70, "e")),
            heights=6,
        )
        tk.Label(right, text="BALANCES", bg=BG, fg=DIM, font=(self.font, 8)).pack(anchor="w")
        self.balances_tree = self._tree(
            right, ("who", "addr", "bal"),
            (("who", "holder", 120, "w"), ("addr", "address", 220, "w"),
             ("bal", "balance", 110, "e")),
            heights=7,
        )

        self.detail = tk.Text(tab, height=5, bg=PANEL, fg=INK, insertbackground=INK,
                              relief="flat", font=self.fn_sm, wrap="word",
                              highlightthickness=1, highlightbackground=LINE)
        self.detail.pack(fill="x", pady=(0, 8))
        self.detail.configure(state="disabled")

    def _build_wallet(self, tab):
        top = tk.Frame(tab, bg=BG)
        top.pack(fill="x", pady=(8, 4))
        tk.Label(top, text="Address", bg=BG, fg=DIM, font=self.fn, width=12,
                 anchor="w").pack(side="left")
        addr = self._entry(top, self.address_var, width=52)
        addr.pack(side="left", fill="x", expand=True, ipady=4)
        addr.configure(state="readonly", readonlybackground=PANEL2, fg=TEAL)
        ttk.Button(top, text="Copy", command=self.copy_address).pack(side="left", padx=6)
        ttk.Button(top, text="Load…", command=self.do_load_wallet).pack(side="left", padx=(0, 6))
        self.new_wallet_btn = ttk.Button(top, text="New wallet", style="Copper.TButton",
                                         command=self.do_new_wallet)
        self.new_wallet_btn.pack(side="left")

        pubrow = tk.Frame(tab, bg=BG)
        pubrow.pack(fill="x", pady=(4, 0))
        tk.Label(pubrow, text="Pubkey", bg=BG, fg=DIM, font=self.fn, width=12,
                 anchor="w").pack(side="left")
        pub = self._entry(pubrow, self.pubkey_var, width=52)
        pub.pack(side="left", fill="x", expand=True, ipady=4)
        pub.configure(state="readonly", readonlybackground=PANEL2, fg=DIM)
        ttk.Button(pubrow, text="Copy", command=self.copy_pubkey).pack(side="left", padx=6)

        tk.Label(tab, textvariable=self.wallet_path_var, bg=BG, fg=DIM2,
                 font=self.fn_sm, anchor="w").pack(fill="x", pady=(2, 0))
        tk.Label(tab, textvariable=self.balance_var, bg=BG, fg=COPPER,
                 font=self.fn_lg, anchor="w").pack(fill="x", pady=(4, 0))
        tk.Label(tab, textvariable=self.mature_var, bg=BG, fg=DIM,
                 font=self.fn_sm, anchor="w").pack(fill="x")
        files = tk.Frame(tab, bg=BG)
        files.pack(fill="x", pady=(4, 0))
        ttk.Button(files, text="Open inbox", command=self.open_inbox_dir).pack(side="left")
        ttk.Button(files, text="Open data folder", command=self.open_data_dir).pack(
            side="left", padx=8)

        tk.Label(tab, text="UNSPENT OUTPUTS", bg=BG, fg=DIM,
                 font=(self.font, 8)).pack(anchor="w", pady=(8, 0))
        self.outputs_tree = self._tree(
            tab, ("out", "value", "h", "tag"),
            (("out", "output", 260, "w"), ("value", "value", 140, "e"),
             ("h", "height", 70, "e"), ("tag", "", 90, "w")),
            heights=5,
        )

        forms = tk.Frame(tab, bg=BG)
        forms.pack(fill="x", pady=(4, 0))
        send = tk.Frame(forms, bg=BG)
        send.pack(side="left", fill="both", expand=True, padx=(0, 12))
        ident = tk.Frame(forms, bg=BG)
        ident.pack(side="left", fill="both", expand=True)

        tk.Label(send, text="SEND", bg=BG, fg=DIM, font=(self.font, 8)).pack(anchor="w")
        self._label_row(send, "To", self.to_var, width=36)
        self._label_row(send, "Amount", self.amount_var, width=16)
        self._label_row(send, "Fee", self.fee_var, width=16)
        self._label_row(send, "Memo", self.memo_var, width=36)
        self.send_btn = ttk.Button(send, text="Sign & write inbox",
                                   style="Copper.TButton", command=self.do_send)
        self.send_btn.pack(anchor="w", pady=(6, 0))

        tk.Label(ident, text="IDENTITY", bg=BG, fg=DIM, font=(self.font, 8)).pack(anchor="w")
        tk.Label(ident, text="Post from the GitHub account you name.\nDisplay only — not consensus.",
                 bg=BG, fg=DIM2, font=self.fn_sm, justify="left").pack(anchor="w", pady=(0, 4))
        self._label_row(ident, "Handle", self.id_handle_var, width=24)
        self.id_btn = ttk.Button(ident, text="Sign identity", command=self.do_identity)
        self.id_btn.pack(anchor="w", pady=(6, 0))

        self._result_box(tab)

    def _build_mine(self, tab):
        tk.Label(tab, text="Your GitHub handle is sealed into the header. A copied\n"
                 "solution answers a puzzle only you were asked.",
                 bg=BG, fg=DIM, font=self.fn_sm, justify="left").pack(anchor="w", pady=(8, 6))
        self._label_row(tab, "Handle", self.handle_var, width=28)
        self._label_row(tab, "Message", self.message_var, width=40)
        self._label_row(tab, "Payout", self.payout_var, width=48)
        src = tk.Frame(tab, bg=BG)
        src.pack(fill="x", pady=4)
        tk.Label(src, text="Source", bg=BG, fg=DIM, font=self.fn, width=12,
                 anchor="w").pack(side="left")
        tk.Radiobutton(src, text="Local chain", variable=self.source_var, value="local",
                       bg=BG, fg=INK, selectcolor=PANEL2, activebackground=BG,
                       activeforeground=INK, font=self.fn,
                       highlightthickness=0).pack(side="left", padx=(0, 12))
        tk.Radiobutton(src, text="Remote repo", variable=self.source_var, value="remote",
                       bg=BG, fg=INK, selectcolor=PANEL2, activebackground=BG,
                       activeforeground=INK, font=self.fn,
                       highlightthickness=0).pack(side="left")
        chk = tk.Frame(tab, bg=BG)
        chk.pack(fill="x", pady=2)
        tk.Checkbutton(chk, text="After each block, open a new GitHub issue with gh",
                       variable=self.submit_var, bg=BG, fg=INK, selectcolor=PANEL2,
                       activebackground=BG, activeforeground=INK, font=self.fn_sm,
                       highlightthickness=0).pack(side="left")
        tk.Checkbutton(chk, text="Keep mining after each block",
                       variable=self.keep_mining_var, bg=BG, fg=INK, selectcolor=PANEL2,
                       activebackground=BG, activeforeground=INK, font=self.fn_sm,
                       highlightthickness=0).pack(side="left", padx=(16, 0))

        btns = tk.Frame(tab, bg=BG)
        btns.pack(fill="x", pady=(8, 6))
        self.start_btn = ttk.Button(btns, text="Start mining", style="Copper.TButton",
                                    command=self.start_mining)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop_mining,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.mine_status = tk.StringVar(value="idle")
        tk.Label(btns, textvariable=self.mine_status, bg=BG, fg=TEAL,
                 font=self.fn).pack(side="left", padx=8)

        self.mine_log = tk.Text(tab, height=12, bg=PANEL, fg=INK, insertbackground=INK,
                                relief="flat", font=self.fn_sm, wrap="word",
                                highlightthickness=1, highlightbackground=LINE)
        self.mine_log.pack(fill="both", expand=True, pady=(0, 8))
        self.mine_log.configure(state="disabled")
        self._result_box(tab, attr="mine_result")

    def _build_verify(self, tab):
        tk.Label(tab, text="Replays every block from genesis. Trusts only the chain file —\n"
                 "not this window, not the README, not the workflow.",
                 bg=BG, fg=DIM, font=self.fn_sm, justify="left").pack(anchor="w", pady=(10, 8))
        self.verify_btn = ttk.Button(tab, text="Verify the chain", style="Copper.TButton",
                                     command=self.do_verify)
        self.verify_btn.pack(anchor="w")
        self.verify_out = tk.Text(tab, height=22, bg=PANEL, fg=INK, insertbackground=INK,
                                  relief="flat", font=self.fn_sm, wrap="word",
                                  highlightthickness=1, highlightbackground=LINE)
        self.verify_out.pack(fill="both", expand=True, pady=10)
        self.verify_out.configure(state="disabled")

    def _result_box(self, parent, attr="result_text"):
        box = tk.Frame(parent, bg=BG)
        box.pack(fill="x", pady=(4, 8))
        text = tk.Text(box, height=4, bg=PANEL, fg=TEAL, insertbackground=INK,
                       relief="flat", font=self.fn_sm, wrap="char",
                       highlightthickness=1, highlightbackground=LINE)
        text.pack(fill="x")
        text.configure(state="disabled")
        setattr(self, attr, text)
        row = tk.Frame(box, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Copy line", command=self.copy_result).pack(side="left")
        ttk.Button(row, text="Open GitHub issue", command=self.open_result_issue).pack(
            side="left", padx=8)
        if attr == "result_text":
            self.result_copy_btn = row.winfo_children()[0]
            self.result_issue_btn = row.winfo_children()[1]

    # ------------------------------------------------------------------ data
    def _set_text(self, widget, value: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _log(self, widget, line: str):
        widget.configure(state="normal")
        widget.insert("end", line + "\n")
        widget.see("end")
        widget.configure(state="disabled")

    def _persist(self):
        self.settings["handle"] = self.handle_var.get().strip()
        self.settings["id_handle"] = self.id_handle_var.get().strip()
        self.settings["repo"] = self.repo_var.get().strip() or DEFAULT_REPO
        self.settings["message"] = self.message_var.get()
        self.settings["submit"] = bool(self.submit_var.get())
        self.settings["keep_mining"] = bool(self.keep_mining_var.get())
        self.settings["source"] = self.source_var.get()
        self.settings["fee"] = self.fee_var.get().strip() or DEFAULT_FEE
        self.settings["to"] = self.to_var.get().strip()
        self.settings["amount"] = self.amount_var.get().strip()
        self.settings["memo"] = self.memo_var.get()
        payout = self.payout_var.get().strip()
        addr = self.address_var.get().strip()
        self.settings["payout"] = "" if payout == addr else payout
        try:
            self.settings["last_tab"] = self.current_tab()
        except Exception:
            pass
        try:
            self.settings["geometry"] = self.root.geometry()
        except tk.TclError:
            pass
        if self.last_result:
            self.settings["last_line"] = self.last_result.get("line", "")
            self.settings["last_title"] = self.last_result.get("title", "")
        self.settings["wallet_path"] = os.path.abspath(self.wallet_path)
        try:
            save_settings(self.settings, self.settings_path)
        except OSError:
            pass

    def _schedule_save(self, *_args):
        if not self._ready:
            return
        if self._save_after is not None:
            try:
                self.root.after_cancel(self._save_after)
            except tk.TclError:
                pass
        try:
            self._save_after = self.root.after(400, self._persist)
        except tk.TclError:
            self._persist()

    def _watch_fields(self):
        for var in (
            self.handle_var, self.id_handle_var, self.message_var, self.repo_var,
            self.source_var, self.to_var, self.amount_var, self.fee_var,
            self.memo_var, self.payout_var,
        ):
            var.trace_add("write", self._schedule_save)
        for var in (self.submit_var, self.keep_mining_var):
            var.trace_add("write", self._schedule_save)
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._schedule_save())

    def _restore_window(self):
        geo = (self.settings.get("geometry") or "").strip()
        if geo:
            try:
                self.root.geometry(geo.split("+")[0] if "x" in geo else "1020x700")
                if "+" in geo:
                    self.root.geometry(geo)
            except tk.TclError:
                pass

    def _restore_last_result(self):
        line = (self.settings.get("last_line") or "").strip()
        if not line:
            return
        self.last_result = {
            "line": line,
            "title": self.settings.get("last_title") or "crux submission",
        }
        try:
            self._set_text(self.result_text, line)
        except tk.TclError:
            pass
        try:
            self._set_text(self.mine_result, line)
        except tk.TclError:
            pass

    def refresh(self):
        if self._ready:
            self._persist()
        wallet = None
        if wallet_present(self.wallet_path):
            try:
                wallet = load_wallet(self.wallet_path)
            except DesktopError as exc:
                self.status_var.set(str(exc))
                wallet = None

        source = self.source_var.get()
        try:
            if source == "remote":
                repo = self.repo_var.get().strip() or DEFAULT_REPO
                self.blocks, self.mempool, self.registry = fetch_remote(repo)
                self.status_var.set(f"following {repo} at height {len(self.blocks) - 1}")
            else:
                self.blocks = chainmod.load_blocks(self.blocks_path)
                self.mempool = load_mempool(self.mempool_path)
                self.registry = load_registry(self.registry_path)
                if self.blocks:
                    self.status_var.set(f"local chain at height {len(self.blocks) - 1}")
                else:
                    self.status_var.set("no local chain — mine genesis or switch to remote")
            self.snap = snapshot(
                blocks=self.blocks,
                registry=self.registry,
                mempool=self.mempool,
                wallet=wallet,
            )
        except (DesktopError, SystemExit, OSError, ValueError) as exc:
            self.status_var.set(str(exc))
            self.snap = snapshot(blocks=[], wallet=wallet)
            self.blocks = []
            self.mempool = []
            self.registry = {}

        if wallet:
            self.settings = autofill_from_wallet(self.settings, wallet, self.registry)
            if not self.handle_var.get().strip() and self.settings.get("handle"):
                self.handle_var.set(self.settings["handle"])
            if not self.id_handle_var.get().strip() and self.settings.get("id_handle"):
                self.id_handle_var.set(self.settings["id_handle"])
            if not self.payout_var.get().strip() and self.settings.get("payout"):
                self.payout_var.set(self.settings["payout"])

        self._paint_stats()
        self._paint_chain()
        self._paint_wallet(wallet)

    def _paint_stats(self):
        s = self.snap
        if s.get("empty"):
            self.height_var.set("—")
            self.diff_var.set("—")
            self.supply_var.set("—")
            self.tip_var.set("—")
            self.height_sub.set("no chain")
            self.diff_sub.set("")
            self.supply_sub.set("")
            self.tip_sub.set("")
            return
        self.height_var.set(str(s["height"]))
        self.height_sub.set(f"{s['tx_count']} txs · {s['utxo_count']} utxos")
        self.diff_var.set(f"{s['difficulty']:,.1f}")
        self.diff_sub.set(f"bits {s['bits']:#010x}")
        self.supply_var.set(f"{format_amount(s['circulating'])}")
        self.supply_sub.set(f"{format_amount(s['next_reward'])} next reward")
        self.tip_var.set(short_hash(s["tip"], 16))
        self.tip_sub.set(s["tip"])

    def _fill_tree(self, tree, rows):
        tree.delete(*tree.get_children())
        for row in rows:
            tree.insert("", "end", values=row)

    def _paint_chain(self):
        s = self.snap
        blocks = []
        for b in s.get("blocks") or []:
            blocks.append((
                b["height"],
                f"@{b['miner']}",
                (b["message"] or "")[:48],
                b["txs"],
                short_hash(b["hash"], 12),
            ))
        self._fill_tree(self.blocks_tree, blocks)

        mem = []
        for t in s.get("mempool") or []:
            mem.append((short_hash(t["txid"], 12), short_hash(t["to"], 14),
                        format_amount(t["amount"])))
        self._fill_tree(self.mempool_tree, mem)

        total = sum(c for _, c in s.get("miners") or []) or 1
        miners = []
        for handle, count in s.get("miners") or []:
            miners.append((f"@{handle}", count, f"{100 * count / total:.1f}%"))
        self._fill_tree(self.miners_tree, miners)

        bals = []
        for addr, handle, value in s.get("balances") or []:
            bals.append((
                f"@{handle}" if handle else "unclaimed",
                addr,
                format_amount(value),
            ))
        self._fill_tree(self.balances_tree, bals)

    def _paint_wallet(self, wallet):
        view = self.snap.get("wallet")
        self.wallet_path_var.set(self.wallet_path)
        if wallet is None:
            self.address_var.set("no wallet — click New wallet")
            self.pubkey_var.set("")
            self.balance_var.set("")
            self.mature_var.set("")
            self.new_wallet_btn.configure(text="New wallet")
            self._fill_tree(self.outputs_tree, [])
            return
        addr = wallet["address"]
        self.address_var.set(addr)
        self.pubkey_var.set(wallet.get("pubkey") or "")
        self.new_wallet_btn.configure(text="Replace wallet")
        if not self.payout_var.get().strip():
            self.payout_var.set(addr)
        if view is None:
            self.balance_var.set("0.00000000 CRUX")
            self.mature_var.set("no unspent outputs on this chain")
            self._fill_tree(self.outputs_tree, [])
            return
        self.balance_var.set(f"{format_amount(view['balance'])} CRUX")
        extra = ""
        if view["handle"]:
            extra = f"  ·  @{view['handle']}"
        self.mature_var.set(
            f"{format_amount(view['mature'])} mature across {len(view['outputs'])} output(s){extra}"
        )
        rows = []
        for o in view["outputs"]:
            tag = ""
            if o["coinbase"] and not o["mature"]:
                tag = "immature"
            elif o["coinbase"]:
                tag = "coinbase"
            rows.append((
                f"{short_hash(o['txid'], 16)}:{o['vout']}",
                f"{format_amount(o['value'])} CRUX",
                o["height"],
                tag,
            ))
        self._fill_tree(self.outputs_tree, rows)

    def select_tab(self, name: str) -> None:
        for tab_id in self.notebook.tabs():
            if self.notebook.tab(tab_id, "text") == name:
                self.notebook.select(tab_id)
                return
        raise KeyError(name)

    def current_tab(self) -> str:
        return self.notebook.tab(self.notebook.select(), "text")

    # ------------------------------------------------------------------ actions
    def open_explorer(self):
        webbrowser.open(EXPLORER_URL)

    def _open_dir(self, path: str):
        os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')  # noqa: S605
        else:
            os.system(f'xdg-open "{path}"')  # noqa: S605

    def open_data_dir(self):
        home = os.path.dirname(os.path.abspath(self.settings_path)) or os.getcwd()
        self._open_dir(home)
        self.status_var.set(f"opened {home}")

    def open_inbox_dir(self):
        self._open_dir(self.inbox_dir)
        self.status_var.set(f"opened {self.inbox_dir}")

    def copy_address(self):
        addr = self.address_var.get()
        if addr.startswith("crux1"):
            self._clip(addr)
            self.status_var.set("address copied")

    def copy_pubkey(self):
        pub = self.pubkey_var.get().strip()
        if pub:
            self._clip(pub)
            self.status_var.set("pubkey copied")

    def _clip(self, text: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def do_new_wallet(self):
        if wallet_present(self.wallet_path):
            if not self.dialogs.askyesno(
                "Overwrite wallet?",
                f"{self.wallet_path} already exists.\n\n"
                "Overwrite it? The old key is gone if you do.",
            ):
                return
            force = True
        else:
            force = False
        try:
            data = create_wallet(self.wallet_path, force=force)
        except DesktopError as exc:
            self.dialogs.showerror("Wallet", str(exc))
            return
        self.status_var.set(f"wallet written · {data['address']}")
        self.payout_var.set(data["address"])
        self.pubkey_var.set(data.get("pubkey") or "")
        self.refresh()

    def do_load_wallet(self):
        path = filedialog.askopenfilename(
            title="Load CRUX wallet",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialdir=os.path.dirname(os.path.abspath(self.wallet_path)) or os.getcwd(),
        )
        if not path:
            return
        try:
            data = load_wallet(path)
        except DesktopError as exc:
            self.dialogs.showerror("Wallet", str(exc))
            return
        self.wallet_path = path
        self.wallet_path_var.set(path)
        self.payout_var.set(data["address"])
        self.pubkey_var.set(data.get("pubkey") or "")
        self.status_var.set(f"loaded {data['address']}")
        self.refresh()
        self._persist()

    def do_send(self):
        try:
            wallet = load_wallet(self.wallet_path)
            result = build_send(
                wallet=wallet,
                to=self.to_var.get(),
                amount=self.amount_var.get(),
                fee=self.fee_var.get() or DEFAULT_FEE,
                memo=self.memo_var.get(),
                blocks=self.blocks,
                inbox_dir=self.inbox_dir,
            )
        except DesktopError as exc:
            self.dialogs.showerror("Send", str(exc))
            self.status_var.set(str(exc))
            return
        self.last_result = result
        self._set_text(self.result_text, result["line"])
        self.status_var.set(f"wrote {result['path']}")
        self._persist()

    def do_identity(self):
        try:
            wallet = load_wallet(self.wallet_path)
            handle = self.id_handle_var.get().strip()
            result = build_identity(wallet=wallet, handle=handle, inbox_dir=self.inbox_dir)
        except DesktopError as orig:
            self.dialogs.showerror("Identity", str(orig))
            self.status_var.set(str(orig))
            return
        self.handle_var.set(result["handle"])
        self.last_result = result
        self._set_text(self.result_text, result["line"])
        self.status_var.set(f"wrote {result['path']}")
        self._persist()

    def copy_result(self):
        if not self.last_result:
            self.status_var.set("nothing to copy yet")
            return
        self._clip(self.last_result["line"])
        self.status_var.set("submission line copied")

    def open_result_issue(self):
        if not self.last_result:
            self.status_var.set("nothing to submit yet")
            return
        repo = self.repo_var.get().strip() or DEFAULT_REPO
        url = issue_url(repo, self.last_result["title"], self.last_result["line"])
        webbrowser.open(url)
        self.status_var.set("opened GitHub issue form")

    def do_search(self):
        q = self.search_var.get().strip().lstrip("@")
        if not q or not self.blocks:
            return
        s = self.snap
        if q.isdigit():
            h = int(q)
            for item in self.blocks_tree.get_children():
                if str(self.blocks_tree.item(item, "values")[0]) == str(h):
                    self.blocks_tree.selection_set(item)
                    self.blocks_tree.see(item)
                    self._show_block(h)
                    return
            self._set_text(self.detail, f"No block at height {h}.")
            return
        if q.lower().startswith("crux1"):
            for item in self.balances_tree.get_children():
                vals = self.balances_tree.item(item, "values")
                if vals and vals[1] == q.lower():
                    self.balances_tree.selection_set(item)
                    self.balances_tree.see(item)
                    self._set_text(self.detail, f"{vals[0]}  {vals[1]}\n{vals[2]} CRUX")
                    return
        low = q.lower()
        for b in s.get("blocks") or []:
            if b["hash"].startswith(low) or b["miner"].lower() == low:
                self._show_block(b["height"])
                return
        self._set_text(self.detail, f"Nothing matched {q!r}.")

    def _on_block_select(self, _evt=None):
        sel = self.blocks_tree.selection()
        if not sel:
            return
        height = int(self.blocks_tree.item(sel[0], "values")[0])
        self._show_block(height)

    def _show_block(self, height: int):
        if height < 0 or height >= len(self.blocks):
            self._set_text(self.detail, f"No block at height {height}.")
            return
        b = self.blocks[height]
        cb = b.txs[0]
        lines = [
            f"block {b.height}  {b.block_hash()}",
            f"miner @{b.miner}  txs {len(b.txs)}  bits {b.bits:#010x}",
            f"message  {cb.coinbase or '—'}",
            f"reward   {format_amount(sum(o.value for o in cb.outputs))} CRUX",
            f"proof    {b.solution}  ({len(bytes.fromhex(b.solution))} bytes)",
        ]
        self._set_text(self.detail, "\n".join(lines))

    def do_verify(self):
        result = verify_chain(blocks=self.blocks)
        if result["ok"]:
            miners = "\n".join(
                f"    {h:<24} {c:>4} block(s)" for h, c in result["miners"]
            )
            text = (
                f"CRUX chain verified\n"
                f"  height        {result['height']}\n"
                f"  tip           {result['tip']}\n"
                f"  blocks        {result['blocks']}\n"
                f"  transactions  {result['tx_count']}\n"
                f"  chainwork     {result['chainwork']:,} expected hashes\n"
                f"  difficulty    {result['difficulty']:,.1f}  (bits {result['bits']:#010x})\n"
                f"  next bits     {result['next_bits']:#010x}\n"
                f"  emitted       {format_amount(result['emitted'])} CRUX\n"
                f"  unspent       {format_amount(result['circulating'])} CRUX across "
                f"{result['utxos']} outputs\n"
                f"\n  miners\n{miners}\n"
                f"\n  all hashes, targets, merkle roots, signatures, knapsacks "
                f"and subsidies check out\n"
            )
            self.status_var.set(f"verified · height {result['height']}")
        else:
            text = f"FAIL  {result['error']}\n"
            self.status_var.set("verification failed")
        self._set_text(self.verify_out, text)
        return result

    def start_mining(self):
        if self.mining:
            return
        handle = self.handle_var.get()
        message = self.message_var.get()
        try:
            validate_miner_fields(handle, message)
        except DesktopError as exc:
            self.dialogs.showerror("Mine", str(exc))
            return
        address = self.payout_var.get().strip()
        if not address:
            if wallet_present(self.wallet_path):
                address = load_wallet(self.wallet_path)["address"]
                self.payout_var.set(address)
            else:
                self.dialogs.showerror("Mine", "create a wallet or paste a payout address")
                return
        self._persist()
        source = self.source_var.get()
        repo = self.repo_var.get().strip() or DEFAULT_REPO
        submit = bool(self.submit_var.get())
        keep = bool(self.keep_mining_var.get())
        self.stop_event.clear()
        self.mining = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.mine_status.set("mining…")
        self._set_text(self.mine_log, "")
        self._log(self.mine_log, f"starting · handle @{handle.strip().lstrip('@')} · {address}")
        thread = threading.Thread(
            target=self._mine_worker,
            args=(handle, address, message, source, repo, submit, keep),
            daemon=True,
        )
        thread.start()

    def stop_mining(self):
        self.stop_event.set()
        self.mine_status.set("stopping…")

    def _mine_worker(self, handle, address, message, source, repo, submit, keep):
        # Runs off the Tk thread. Do not touch widgets or StringVars here.
        try:
            if source == "remote":
                blocks, mempool, _reg = fetch_remote(repo)
            else:
                blocks = list(self.blocks)
                mempool = list(self.mempool)
            if not blocks:
                self.queue.put(("error", "no chain to extend — load local genesis or switch to remote"))
                return
            while True:
                result = mine_block(
                    blocks,
                    handle,
                    address,
                    message=message,
                    mempool=mempool,
                    stop=self.stop_event.is_set,
                    on_progress=lambda p: self.queue.put(("progress", p)),
                    quiet=True,
                    inbox_dir=self.inbox_dir,
                )
                if result is None:
                    self.queue.put(("stopped", None))
                    return
                if submit:
                    try:
                        ok = submit_block(repo, result["block"], result["line"])
                        result["submitted"] = ok
                    except DesktopError as exc:
                        result["submitted"] = False
                        result["submit_error"] = str(exc)
                more = bool(keep) and not self.stop_event.is_set()
                result["continue"] = more
                self.queue.put(("found", result))
                if not more:
                    return
                if source == "remote":
                    try:
                        blocks, mempool, _reg = fetch_remote(repo)
                    except (DesktopError, SystemExit) as exc:
                        self.queue.put(("error", str(exc)))
                        return
                else:
                    try:
                        from crux.chain import append_block
                        append_block(result["block"], path=self.blocks_path)
                    except OSError:
                        pass
                    blocks = list(blocks) + [result["block"]]
                    mined_ids = {t.txid() for t in result["block"].txs}
                    mempool = [t for t in mempool if t.txid() not in mined_ids]
        except (DesktopError, SystemExit) as exc:
            self.queue.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", f"{type(exc).__name__}: {exc}"))

    def _pump(self):
        if not self._pumping:
            return
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if self._pumping:
            try:
                self._pump_after = self.root.after(200, self._pump)
            except tk.TclError:
                return

    def _handle_event(self, kind, payload):
        if kind == "progress":
            p = payload
            if p.get("event") == "progress":
                self.mine_status.set(
                    f"nonce {p['nonce']:,}  {p['rate']:.2f}/s  {p['solved']} solved  {p['elapsed']:.0f}s"
                )
                self._log(self.mine_log,
                          f"  nonce {p['nonce']:,}  {p['rate']:.2f} puzzles/s  "
                          f"{p['solved']} solved  {p['elapsed']:.0f}s")
            return
        if kind == "found":
            self.last_result = payload
            self._log(self.mine_log, f"found  {payload['hash']}")
            self._log(self.mine_log, f"wrote  {payload['path']}")
            if payload.get("submitted"):
                self._log(self.mine_log, "submitted as a new GitHub issue")
            elif payload.get("submit_error"):
                self._log(self.mine_log, payload["submit_error"])
            self._set_text(self.mine_result, payload["line"])
            if payload.get("continue"):
                self.mine_status.set(f"found height {payload['height']}; mining next…")
                self.status_var.set(f"mined block {payload['height']}; continuing")
                return
            self.mining = False
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.mine_status.set(f"found height {payload['height']}")
            self.status_var.set(f"mined block {payload['height']}")
            return
        if kind == "stopped":
            self.mining = False
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.mine_status.set("stopped")
            self._log(self.mine_log, "stopped")
            return
        if kind == "error":
            self.mining = False
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.mine_status.set("error")
            self._log(self.mine_log, str(payload))
            self.status_var.set(str(payload))

    def shutdown(self):
        self._ready = False
        self._pumping = False
        for attr in ("_save_after", "_pump_after"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        self.stop_event.set()

    def _on_close(self):
        self.shutdown()
        try:
            self._persist()
        except tk.TclError:
            pass
        self.root.destroy()


def main(argv=None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="CRUX desktop GUI")
    ap.add_argument("--wallet", default=None, help="path to crux-wallet.json")
    ap.add_argument("--settings", default=None, help="path to crux-gui.json")
    ap.add_argument("--inbox", default=None, help="directory for submission files")
    ap.add_argument("--repo", default="", help="owner/name shown in the header")
    ap.add_argument("--version", action="version", version=f"CRUX {__version__}")
    args = ap.parse_args(argv)

    paths = default_runtime_paths()
    os.makedirs(paths["inbox_dir"], exist_ok=True)
    settings = args.settings or paths["settings_path"]
    saved = load_settings(settings)
    preferred = args.wallet or saved.get("wallet_path") or paths["wallet_path"]
    wallet = resolve_wallet_path(preferred)
    inbox = args.inbox or paths["inbox_dir"]

    try:
        root = tk.Tk()
        App(
            root,
            wallet_path=wallet,
            settings_path=settings,
            inbox_dir=inbox,
            blocks_path=paths["blocks_path"],
            registry_path=paths["registry_path"],
            mempool_path=paths["mempool_path"],
            repo=args.repo or None,
        )
        root.mainloop()
        return 0
    except Exception as exc:
        log_path = ""
        try:
            home = user_data_dir()
            os.makedirs(home, exist_ok=True)
            log_path = os.path.join(home, "crux-crash.log")
            import traceback

            with open(log_path, "w", encoding="utf-8") as fh:
                traceback.print_exc(file=fh)
        except OSError:
            pass
        if frozen():
            try:
                detail = f"{exc}"
                if log_path:
                    detail += f"\n\nLogged to {log_path}"
                messagebox.showerror("CRUX", detail)
            except Exception:
                pass
        else:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
