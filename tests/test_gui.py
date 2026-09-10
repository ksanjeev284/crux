#!/usr/bin/env python3
"""
GUI tests for CRUX.

The operations layer (`crux.desktop`) is tested without a display. Widget
smoke tests construct the Tk app when tkinter can open a window, and skip
cleanly when it cannot (headless CI without xvfb).

    python3 tests/test_gui.py
"""

import json
import os
import queue
import secrets
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crux import chain as chainmod
from crux import consensus as k
from crux import crypto
from crux import pow as rp
from crux.consensus import format_amount
from crux import __version__
from crux.desktop import (
    DEFAULT_REPO,
    DesktopError,
    build_identity,
    build_send,
    create_wallet,
    default_settings,
    issue_url,
    load_settings,
    load_wallet,
    mine_block,
    parse_amount,
    save_settings,
    snapshot,
    validate_miner_fields,
    verify_chain,
    verify_identity_line,
    wallet_present,
    write_inbox,
)
from crux import desktop as deskmod
from crux import paths as pathmod
from crux.wire import BLOCK_PREFIX, ID_PREFIX, TX_PREFIX, find_payload

PASSED = []
BASE_TIME = 1_750_000_000
ORIG_N, ORIG_B = rp.N, rp.B
ORIG_POW, ORIG_GEN = k.POW_LIMIT_BITS, k.GENESIS_BITS
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ok(label):
    PASSED.append(label)
    print(f"  ok    {label}")


def expect_error(label, fn, exc_type=DesktopError, match=None):
    try:
        fn()
    except exc_type as exc:
        msg = str(exc)
        if match and match not in msg.lower() and match not in msg:
            raise AssertionError(f"{label}: {msg!r} did not contain {match!r}")
        short = msg if len(msg) < 88 else msg[:85] + "…"
        print(f"  ok    {label}\n          rejected: {short}")
        PASSED.append(label)
        return
    raise AssertionError(f"FAILED: {label} was accepted but should have been rejected")


def new_key():
    priv_bytes = secrets.token_bytes(32)
    priv = crypto.privkey_from_bytes(priv_bytes)
    pub = crypto.ser_pubkey(crypto.pubkey(priv))
    return priv, pub.hex(), crypto.pubkey_to_address(pub)


def shrink_pow():
    rp.N = 16
    rp.B = rp.N - 2
    k.POW_LIMIT_BITS = 0x2100FFFF
    k.GENESIS_BITS = k.POW_LIMIT_BITS


def restore_pow():
    rp.N, rp.B = ORIG_N, ORIG_B
    k.POW_LIMIT_BITS, k.GENESIS_BITS = ORIG_POW, ORIG_GEN


def mine_test_block(blocks, miner, address, txs=None, message="", timestamp=None):
    txs = txs or []
    height = len(blocks)
    state = chainmod.replay(blocks, strict_time=False)
    bits = chainmod.bits_for_height(height, blocks)
    target = k.bits_to_target(bits)
    fees = 0
    working = state.utxos.copy()
    for t in txs:
        fees += k.validate_tx(t, working, height)
        for i in t.inputs:
            working.spend(i.txid, i.vout)
        working.add_tx(t, height)
    coinbase = k.Tx(
        coinbase=message,
        cb_height=height,
        outputs=[k.TxOut(k.block_subsidy(height) + fees, address)],
    )
    all_txs = [coinbase] + txs
    ts = timestamp if timestamp is not None else BASE_TIME + height * 600
    block = k.Block(
        height=height,
        prev_hash=state.tip_hash,
        merkle_root=k.merkle_root([t.txid() for t in all_txs]),
        timestamp=ts,
        bits=bits,
        miner=miner,
        txs=all_txs,
    )
    nonce = 0
    while True:
        block.nonce = nonce
        numbers, tgt = rp.instance(block.header_core())
        subset = rp.solve_instance(numbers, tgt)
        if subset is not None:
            block.solution = rp.encode_solution(subset)
            if rp.hash_meets_target(block.digest(), target):
                return block
        nonce += 1
        if nonce >= rp.MAX_NONCE:
            raise RuntimeError("could not mine a test block")


def mature_chain(alice_addr, n_blocks=12):
    blocks = [mine_test_block([], "crux", alice_addr, message="gui genesis")]
    for _ in range(1, n_blocks):
        blocks.append(mine_test_block(blocks, "crux", alice_addr))
    return blocks


def have_display():
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        root.destroy()
        return True
    except Exception:
        return False


class FakeDialogs:
    def __init__(self, yes=True):
        self.yes = yes
        self.errors = []
        self.questions = []

    def showerror(self, title, msg):
        self.errors.append((title, msg))

    def askyesno(self, title, msg):
        self.questions.append((title, msg))
        return self.yes


def run_headless():
    print("CRUX GUI operations")
    print()

    # ---- parse_amount --------------------------------------------------
    assert parse_amount("1") == k.COIN
    assert parse_amount("1.5") == k.COIN + k.COIN // 2
    assert parse_amount("0.00000001") == 1
    assert parse_amount("0") == 0
    assert parse_amount("50.00000000") == 50 * k.COIN
    ok("parse_amount accepts whole, fractional and 8-decimal values")

    expect_error("empty amount rejected", lambda: parse_amount(""), match="empty")
    expect_error("negative amount rejected", lambda: parse_amount("-1"), match="non-negative")
    expect_error("too many decimals rejected", lambda: parse_amount("1.123456789"), match="8 decimal")
    expect_error("letters rejected", lambda: parse_amount("nope"), match="invalid")
    expect_error("blank fraction rejected", lambda: parse_amount("1."), match="invalid")

    # ---- wallet files --------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "crux-wallet.json")
        assert wallet_present(path) is False
        w = create_wallet(path)
        assert wallet_present(path)
        assert crypto.address_is_valid(w["address"])
        assert len(bytes.fromhex(w["privkey"])) == 32
        loaded = load_wallet(path)
        assert loaded["address"] == w["address"]
        ok("create_wallet writes a valid key and address")

        expect_error("refuses to overwrite without force", lambda: create_wallet(path))
        w2 = create_wallet(path, force=True)
        assert w2["address"] != w["address"]
        ok("create_wallet --force overwrites with a new key")

        expect_error("missing wallet is a DesktopError", lambda: load_wallet(os.path.join(tmp, "nope.json")))

    # ---- settings ------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "crux-gui.json")
        s = load_settings(path)
        assert s == default_settings()
        s["handle"] = "octocat"
        s["repo"] = "octocat/crux"
        save_settings(s, path)
        back = load_settings(path)
        assert back["handle"] == "octocat"
        assert back["repo"] == "octocat/crux"
        ok("settings round-trip")

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert load_settings(path)["repo"] == DEFAULT_REPO
        ok("corrupt settings file falls back to defaults")

    # ---- issue URL -----------------------------------------------------
    url = issue_url("ksanjeev284/crux", "crux block 1 abc", "crux-block-v1:xyz")
    assert url.startswith("https://github.com/ksanjeev284/crux/issues/new")
    assert "labels=crux" in url
    assert "crux-block-v1" in url
    expect_error("repo without slash rejected", lambda: issue_url("nope", "t", "b"))
    ok("GitHub issue URL is well-formed")

    # ---- miner field validation ----------------------------------------
    validate_miner_fields("octocat", "gm")
    expect_error("empty miner rejected", lambda: validate_miner_fields("", "gm"))
    expect_error("long miner rejected", lambda: validate_miner_fields("x" * 40, "gm"))
    expect_error("bad miner chars rejected", lambda: validate_miner_fields("not_a_user", "gm"))
    expect_error("pipe in message rejected", lambda: validate_miner_fields("octocat", "a|b"))
    ok("miner handle and message share consensus checks")

    # ---- live chain snapshot matches verify.py -------------------------
    live_blocks = chainmod.load_blocks(os.path.join(ROOT, "chain", "blocks.jsonl"))
    live_snap = snapshot(
        blocks=live_blocks,
        registry_path=os.path.join(ROOT, "chain", "registry.json"),
        mempool_path=os.path.join(ROOT, "chain", "mempool.jsonl"),
    )
    live_ver = verify_chain(blocks=live_blocks)
    assert live_ver["ok"], live_ver
    assert live_snap["height"] == live_ver["height"]
    assert live_snap["tip"] == live_ver["tip"]
    assert live_snap["circulating"] == live_ver["circulating"]
    assert live_snap["emitted"] == live_ver["emitted"]
    assert live_snap["chainwork"] == live_ver["chainwork"]
    assert live_snap["emitted"] == live_snap["circulating"]
    ok(f"snapshot of live chain agrees with verify (height {live_ver['height']})")

    # ---- tampered chain fails verify -----------------------------------
    if live_blocks:
        cloned = k.Block.from_dict(live_blocks[0].to_dict())
        cloned.nonce += 1
        result = verify_chain(blocks=[cloned])
        assert result["ok"] is False
        assert result.get("error")
        ok("verify_chain reports failure on a tampered block")

    empty = verify_chain(blocks=[])
    assert empty["ok"] is False
    ok("verify_chain fails on an empty chain")

    # ---- send / identity on a mined test chain -------------------------
    shrink_pow()
    try:
        alice_priv, alice_pub, alice_addr = new_key()
        _bob_priv, _bob_pub, bob_addr = new_key()
        blocks = mature_chain(alice_addr, 12)
        state = chainmod.replay(blocks, now=BASE_TIME + 200 * 600, strict_time=False)
        assert state.height == 11
        ok("test chain of 12 blocks replays for GUI send tests")

        with tempfile.TemporaryDirectory() as tmp:
            wpath = os.path.join(tmp, "w.json")
            inbox = os.path.join(tmp, "inbox")
            wallet = {
                "privkey": int(alice_priv).to_bytes(32, "big").hex(),
                "pubkey": alice_pub,
                "address": alice_addr,
            }
            with open(wpath, "w", encoding="utf-8") as fh:
                json.dump(wallet, fh)

            snap = snapshot(blocks=blocks, wallet=wallet, now=BASE_TIME + 200 * 600)
            assert snap["wallet"]["address"] == alice_addr
            assert snap["wallet"]["balance"] > 0
            assert snap["wallet"]["mature"] > 0
            ok("snapshot exposes mature wallet balance")

            result = build_send(
                wallet=wallet,
                to=bob_addr,
                amount="1.5",
                fee="0.001",
                memo="gg",
                blocks=blocks,
                inbox_dir=inbox,
                now=BASE_TIME + 200 * 600,
            )
            assert result["txid"]
            assert result["line"].startswith(TX_PREFIX)
            assert os.path.exists(result["path"])
            kind, _payload = find_payload(result["line"])
            assert kind == "tx"
            working = chainmod.replay(blocks, now=BASE_TIME + 200 * 600, strict_time=False)
            k.validate_tx(result["tx"], working.utxos, working.height + 1)
            ok("GUI send writes a valid crux-tx-v1 line that consensus accepts")

            expect_error(
                "invalid destination rejected",
                lambda: build_send(wallet=wallet, to="not-an-address", amount="1",
                                   blocks=blocks, inbox_dir=inbox),
                match="invalid destination",
            )
            expect_error(
                "zero amount rejected",
                lambda: build_send(wallet=wallet, to=bob_addr, amount="0",
                                   blocks=blocks, inbox_dir=inbox),
                match="greater than zero",
            )
            expect_error(
                "insufficient funds rejected",
                lambda: build_send(wallet=wallet, to=bob_addr, amount="999999",
                                   blocks=blocks, inbox_dir=inbox),
                match="insufficient",
            )
            expect_error(
                "pipe in memo rejected",
                lambda: build_send(wallet=wallet, to=bob_addr, amount="0.1",
                                   memo="a|b", blocks=blocks, inbox_dir=inbox),
            )

            ident = build_identity(wallet=wallet, handle="octocat", inbox_dir=inbox)
            assert ident["line"].startswith(ID_PREFIX)
            assert os.path.exists(ident["path"])
            assert verify_identity_line(ident["line"], wallet)
            ok("identity line verifies against the wallet key")

            expect_error("empty identity handle rejected",
                         lambda: build_identity(wallet=wallet, handle="", inbox_dir=inbox))
            expect_error("identity handle with underscore rejected",
                         lambda: build_identity(wallet=wallet, handle="not_ok", inbox_dir=inbox))
            assert verify_identity_line("not a line") is False
            ok("malformed identity line is not verified")

            # immature coinbase cannot be spent from a 1-block chain
            young = mature_chain(alice_addr, 1)
            expect_error(
                "immature coinbase is not spendable via the GUI",
                lambda: build_send(wallet=wallet, to=bob_addr, amount="1",
                                   blocks=young, inbox_dir=inbox,
                                   now=BASE_TIME + 200 * 600),
                match="insufficient",
            )

        # ---- mine_block via desktop, with stop --------------------------
        with tempfile.TemporaryDirectory() as tmp:
            inbox = os.path.join(tmp, "inbox")
            found = mine_block(
                blocks,
                "octocat",
                bob_addr,
                message="gui mine",
                mempool=[],
                now=BASE_TIME + 12 * 600,
                quiet=True,
                inbox_dir=inbox,
            )
            assert found is not None
            assert found["line"].startswith(BLOCK_PREFIX)
            assert os.path.exists(found["path"])
            chainmod.replay(blocks + [found["block"]], now=BASE_TIME + 200 * 600)
            ok("desktop mine_block produces a block the chain accepts")
            assert len(bytes.fromhex(found["block"].solution)) == rp.SOLUTION_BYTES
            ok("GUI-mined proof is still 8 bytes")

            stopped = mine_block(
                blocks,
                "octocat",
                bob_addr,
                message="stop me",
                now=BASE_TIME + 12 * 600,
                stop=lambda: True,
                quiet=True,
                inbox_dir=inbox,
                write=False,
            )
            assert stopped is None
            ok("stop callback aborts mining and returns None")

            progressed = []
            mine_block(
                blocks,
                "octocat",
                bob_addr,
                now=BASE_TIME + 12 * 600,
                on_progress=lambda p: progressed.append(p),
                quiet=True,
                inbox_dir=inbox,
                write=False,
            )
            assert any(p.get("event") == "found" for p in progressed)
            ok("on_progress fires a found event")

            expect_error(
                "invalid payout address rejected before mining",
                lambda: mine_block(blocks, "octocat", "nope", quiet=True, write=False),
            )
    finally:
        restore_pow()

    # ---- inbox helper --------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        path = write_inbox("tx", "deadbeef", "crux-tx-v1:hi", inbox_dir=tmp)
        with open(path, encoding="utf-8") as fh:
            assert fh.read() == "crux-tx-v1:hi\n"
        ok("write_inbox stores the submission line")

    # ---- runtime paths -------------------------------------------------
    assert pathmod.frozen() is False
    assert default_settings()["source"] == "local"
    paths = pathmod.default_runtime_paths()
    assert paths["wallet_path"].endswith("crux-wallet.json")
    assert os.path.basename(paths["inbox_dir"]) == "inbox"
    assert paths["home"] == os.getcwd()
    ok("source-tree runtime paths stay in the working directory")

    orig_desk = deskmod.frozen
    orig_path = pathmod.frozen
    deskmod.frozen = lambda: True
    pathmod.frozen = lambda: True
    try:
        assert deskmod.default_settings()["source"] == "remote"
        home = pathmod.user_data_dir()
        assert home != os.getcwd()
        assert "CRUX" in home or "crux" in home
    finally:
        deskmod.frozen = orig_desk
        pathmod.frozen = orig_path
    ok("frozen executable follows the remote repo and uses a user data dir")

    # ---- gui.py --help / --version do not open a window ---------------
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "gui.py"), "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "wallet" in proc.stdout.lower()
    ok("gui.py --help exits 0 without opening a window")

    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "gui.py"), "--version"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert __version__ in proc.stdout
    ok(f"gui.py --version prints CRUX {__version__}")


def run_widgets():
    print()
    print("CRUX GUI widgets")
    print()
    if not have_display():
        print("  skip  no display (tkinter could not open a window)")
        ok("widget tests skipped without a display")
        return

    import tkinter as tk
    import gui as guimod

    live_blocks_path = os.path.join(ROOT, "chain", "blocks.jsonl")
    live_reg = os.path.join(ROOT, "chain", "registry.json")
    live_mem = os.path.join(ROOT, "chain", "mempool.jsonl")

    with tempfile.TemporaryDirectory() as tmp:
        wallet_path = os.path.join(tmp, "crux-wallet.json")
        settings_path = os.path.join(tmp, "crux-gui.json")
        inbox = os.path.join(tmp, "inbox")
        root = tk.Tk()
        root.withdraw()
        app = guimod.App(
            root,
            wallet_path=wallet_path,
            settings_path=settings_path,
            blocks_path=live_blocks_path,
            registry_path=live_reg,
            mempool_path=live_mem,
            inbox_dir=inbox,
            repo="ksanjeev284/crux",
        )
        app.dialogs = FakeDialogs()
        root.update_idletasks()

        tabs = [app.notebook.tab(t, "text") for t in app.notebook.tabs()]
        assert tabs == ["Chain", "Wallet", "Mine", "Verify"], tabs
        ok("notebook has Chain, Wallet, Mine, Verify")

        live = verify_chain(blocks_path=live_blocks_path)
        assert app.height_var.get() == str(live["height"])
        assert short_ok(app.tip_var.get(), live["tip"])
        ok("chain tab stats match an independent verify")

        assert app.blocks_tree.get_children()
        ok("blocks tree is populated")
        assert app.miners_tree.get_children()
        ok("miners tree is populated")
        assert app.balances_tree.get_children()
        ok("balances tree is populated")

        app.select_tab("Wallet")
        assert app.current_tab() == "Wallet"
        assert "no wallet" in app.address_var.get()
        ok("wallet tab shows create-wallet state")

        app.do_new_wallet()
        root.update_idletasks()
        addr = app.address_var.get()
        assert addr.startswith("crux1")
        assert crypto.address_is_valid(addr)
        assert os.path.exists(wallet_path)
        ok("New wallet button writes a wallet and shows the address")

        app.copy_address()
        try:
            clipped = root.clipboard_get()
            assert clipped == addr
            ok("Copy puts the address on the clipboard")
        except tk.TclError:
            ok("copy path exercised (clipboard not available in this environment)")

        app.to_var.set("not-an-address")
        app.amount_var.set("1")
        app.do_send()
        assert app.dialogs.errors
        assert "invalid" in app.dialogs.errors[-1][1].lower()
        ok("send from the wallet tab rejects a bad address without crashing")

        app.id_handle_var.set("gui-tester")
        app.do_identity()
        line = app.result_text.get("1.0", "end").strip()
        assert line.startswith(ID_PREFIX)
        assert app.last_result["handle"] == "gui-tester"
        ok("identity from the wallet tab writes a crux-id-v1 line")

        app.search_var.set("0")
        app.select_tab("Chain")
        app.do_search()
        detail = app.detail.get("1.0", "end")
        assert "block 0" in detail
        ok("search by height shows genesis in the detail pane")

        app.select_tab("Verify")
        result = app.do_verify()
        assert result["ok"] is True
        out = app.verify_out.get("1.0", "end")
        assert "chain verified" in out
        assert str(live["height"]) in out
        ok("verify tab replays the live chain and reports ok")

        app.select_tab("Mine")
        assert str(app.start_btn["state"]) == "normal"
        assert str(app.stop_btn["state"]) == "disabled"
        app.handle_var.set("")
        app.start_mining()
        assert app.dialogs.errors[-1][0] == "Mine"
        assert app.mining is False
        ok("start mining with an empty handle is rejected")

        app._pumping = False
        app.stop_event.set()
        root.destroy()

    # second app: existing wallet, send a real tx against a tiny chain
    shrink_pow()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            alice_priv, alice_pub, alice_addr = new_key()
            _bpriv, _bpub, bob_addr = new_key()
            blocks = mature_chain(alice_addr, 12)
            bpath = os.path.join(tmp, "blocks.jsonl")
            with open(bpath, "w", encoding="utf-8") as fh:
                for b in blocks:
                    fh.write(json.dumps(b.to_dict(), separators=(",", ":"), sort_keys=True) + "\n")
            wpath = os.path.join(tmp, "w.json")
            wallet = {
                "version": 1,
                "privkey": int(alice_priv).to_bytes(32, "big").hex(),
                "pubkey": alice_pub,
                "address": alice_addr,
            }
            with open(wpath, "w", encoding="utf-8") as fh:
                json.dump(wallet, fh)
            root = tk.Tk()
            root.withdraw()
            app = guimod.App(
                root,
                wallet_path=wpath,
                settings_path=os.path.join(tmp, "s.json"),
                blocks_path=bpath,
                registry_path=os.path.join(tmp, "missing-registry.json"),
                mempool_path=os.path.join(tmp, "missing-mempool.jsonl"),
                inbox_dir=os.path.join(tmp, "inbox"),
            )
            app.dialogs = FakeDialogs()
            root.update_idletasks()
            assert app.address_var.get() == alice_addr
            assert "CRUX" in app.balance_var.get()
            ok("wallet tab shows balance from a local test chain")

            app.to_var.set(bob_addr)
            app.amount_var.set("1.25")
            app.fee_var.set("0.001")
            app.memo_var.set("from the gui")
            app.do_send()
            assert app.last_result is not None
            assert app.last_result["line"].startswith(TX_PREFIX)
            assert os.path.exists(app.last_result["path"])
            k.validate_tx(
                app.last_result["tx"],
                chainmod.replay(blocks, now=BASE_TIME + 200 * 600, strict_time=False).utxos,
                12,
            )
            ok("wallet tab send produces a consensus-valid transaction")

            app.select_tab("Mine")
            app.handle_var.set("octocat")
            app.start_mining()
            assert app.mining is True
            assert str(app.start_btn["state"]) == "disabled"
            assert str(app.stop_btn["state"]) == "normal"
            ok("start mining enables Stop and disables Start")
            deadline = time.time() + 30
            while time.time() < deadline:
                root.update()
                try:
                    while True:
                        kind, payload = app.queue.get_nowait()
                        app._handle_event(kind, payload)
                except queue.Empty:
                    pass
                if not app.mining:
                    break
                time.sleep(0.05)
            log = app.mine_log.get("1.0", "end")
            line = (app.last_result or {}).get("line", "")
            assert line.startswith(BLOCK_PREFIX), (
                f"mining did not produce a block\n"
                f"  mining={app.mining} status={app.mine_status.get()!r}\n"
                f"  last={line[:80]!r}\n"
                f"  log={log!r}"
            )
            assert os.path.exists(app.last_result["path"])
            chainmod.replay(
                blocks + [app.last_result["block"]],
                strict_time=False,
            )
            ok("mine tab finds a block the chain accepts")

            app._pumping = False
            app.stop_event.set()
            root.destroy()
    finally:
        restore_pow()


def short_ok(shown: str, full: str) -> bool:
    if shown == full:
        return True
    if shown.endswith("…"):
        return full.startswith(shown[:-1])
    return False


def run():
    print("CRUX GUI tests")
    print()
    run_headless()
    run_widgets()
    print()
    print(f"  {len(PASSED)} checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    finally:
        restore_pow()
