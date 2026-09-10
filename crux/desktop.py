"""
Headless operations for the CRUX desktop GUI.

Standard library only. Nothing here talks to Tk; the GUI and the GUI tests
both call these functions so a missing display cannot skip the real checks.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import webbrowser
from urllib.parse import quote

from . import chain as chainmod
from . import crypto
from .consensus import (
    COIN,
    COINBASE_MATURITY,
    HALVING_INTERVAL,
    RETARGET_INTERVAL,
    ConsensusError,
    Tx,
    TxIn,
    TxOut,
    block_subsidy,
    check_memo,
    check_message,
    check_miner_name,
    difficulty,
    format_amount,
    validate_tx,
)
from .wire import ID_PREFIX, encode_block, encode_tx

WALLET_FILE = "crux-wallet.json"
SETTINGS_FILE = "crux-gui.json"
DEFAULT_REPO = "ksanjeev284/crux"
DEFAULT_FEE = "0.001"
EXPLORER_URL = "https://ksanjeev284.github.io/crux/"


class DesktopError(Exception):
    """User-facing error from a GUI operation. Never a consensus failure."""


# --------------------------------------------------------------------------
# amounts, paths
# --------------------------------------------------------------------------


def parse_amount(s: str) -> int:
    """Convert a decimal CRUX string into an integer number of grains."""
    raw = (s or "").strip()
    if not raw:
        raise DesktopError("amount is empty")
    if raw[0] in "+-":
        raise DesktopError("amount must be non-negative")
    if "." not in raw:
        if not raw.isdigit():
            raise DesktopError("invalid amount")
        return int(raw) * COIN
    whole, frac = raw.split(".", 1)
    if whole and not whole.isdigit():
        raise DesktopError("invalid amount")
    if not frac or not frac.isdigit():
        raise DesktopError("invalid amount")
    if len(frac) > 8:
        raise DesktopError("amounts have at most 8 decimal places")
    return int(whole or "0") * COIN + int(frac.ljust(8, "0"))


def write_inbox(kind: str, name: str, line: str, inbox_dir: str = "inbox") -> str:
    os.makedirs(inbox_dir, exist_ok=True)
    path = os.path.join(inbox_dir, f"{kind}-{name}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path


def issue_url(repo: str, title: str, body: str) -> str:
    repo = (repo or DEFAULT_REPO).strip()
    if "/" not in repo:
        raise DesktopError("repo must look like owner/name")
    return (
        f"https://github.com/{repo}/issues/new"
        f"?labels=crux&title={quote(title)}&body={quote(body)}"
    )


def open_issue_in_browser(repo: str, title: str, body: str) -> str:
    url = issue_url(repo, title, body)
    webbrowser.open(url)
    return url


def short_hash(value: str, n: int = 12) -> str:
    if not value:
        return ""
    if len(value) <= n + 1:
        return value
    return value[:n] + "…"


# --------------------------------------------------------------------------
# wallet
# --------------------------------------------------------------------------


def wallet_present(path: str = WALLET_FILE) -> bool:
    return os.path.exists(path)


def load_wallet(path: str = WALLET_FILE) -> dict:
    if not os.path.exists(path):
        raise DesktopError(f"no wallet at {path}. create one first.")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "privkey" not in data or "address" not in data:
        raise DesktopError(f"{path} is not a CRUX wallet")
    return data


def create_wallet(path: str = WALLET_FILE, force: bool = False) -> dict:
    if os.path.exists(path) and not force:
        raise DesktopError(f"{path} already exists")
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    priv_bytes = secrets.token_bytes(32)
    priv = crypto.privkey_from_bytes(priv_bytes)
    pub = crypto.ser_pubkey(crypto.pubkey(priv))
    address = crypto.pubkey_to_address(pub)
    data = {
        "version": 1,
        "privkey": priv_bytes.hex(),
        "pubkey": pub.hex(),
        "address": address,
        "warning": "CRUX testnet toy key. Worth nothing. Never reuse this key.",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return data


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


def default_settings() -> dict:
    return {
        "handle": "",
        "repo": DEFAULT_REPO,
        "message": "gm",
        "submit": False,
        "source": "local",
        "fee": DEFAULT_FEE,
    }


def load_settings(path: str = SETTINGS_FILE) -> dict:
    data = default_settings()
    if not os.path.exists(path):
        return data
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return data
    if not isinstance(raw, dict):
        return data
    for key in data:
        if key in raw and isinstance(raw[key], type(data[key])):
            data[key] = raw[key]
    return data


def save_settings(data: dict, path: str = SETTINGS_FILE) -> None:
    out = default_settings()
    out.update({k: data[k] for k in out if k in data})
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)


# --------------------------------------------------------------------------
# chain
# --------------------------------------------------------------------------


def load_registry(path: str = os.path.join("chain", "registry.json")) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def load_mempool(path: str = os.path.join("chain", "mempool.jsonl")) -> list:
    if not os.path.exists(path):
        return []
    txs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                txs.append(Tx.from_dict(json.loads(line)))
    return txs


def _handle_for(address: str, registry: dict) -> str:
    for handle, rec in registry.items():
        if isinstance(rec, dict) and rec.get("address") == address:
            return handle
    return ""


def _block_reward(block) -> int:
    return sum(o.value for o in block.txs[0].outputs)


def snapshot(
    blocks=None,
    registry=None,
    mempool=None,
    wallet=None,
    blocks_path: str | None = None,
    registry_path: str | None = None,
    mempool_path: str | None = None,
    now: int | None = None,
) -> dict:
    """Replay the chain and return a JSON-friendly overview for the GUI."""
    if blocks is None:
        blocks = chainmod.load_blocks(blocks_path or chainmod.BLOCKS_FILE)
    if registry is None:
        registry = load_registry(registry_path or os.path.join("chain", "registry.json"))
    if mempool is None:
        mempool = load_mempool(mempool_path or os.path.join("chain", "mempool.jsonl"))

    if not blocks:
        return {
            "empty": True,
            "height": -1,
            "tip": "",
            "difficulty": 0.0,
            "bits": 0,
            "chainwork": 0,
            "emitted": 0,
            "circulating": 0,
            "tx_count": 0,
            "utxo_count": 0,
            "next_reward": block_subsidy(0),
            "next_retarget": RETARGET_INTERVAL,
            "next_halving": HALVING_INTERVAL,
            "miners": [],
            "balances": [],
            "blocks": [],
            "mempool": [],
            "wallet": None,
            "problems": ["chain is empty"],
        }

    state = chainmod.replay(blocks, now=now, strict_time=False)
    emitted = chainmod.emitted_supply(state.height)
    balances_map = state.utxos.balances()
    height = state.height

    miners = sorted(state.miners.items(), key=lambda p: (-p[1], p[0]))
    balances = sorted(
        (
            (addr, _handle_for(addr, registry), value)
            for addr, value in balances_map.items()
        ),
        key=lambda row: (-row[2], row[0]),
    )
    recent = []
    for b in blocks[-16:]:
        recent.append({
            "height": b.height,
            "hash": b.block_hash(),
            "miner": b.miner,
            "message": b.txs[0].coinbase if b.txs else "",
            "txs": len(b.txs),
            "reward": _block_reward(b),
            "timestamp": b.timestamp,
            "bits": b.bits,
        })
    recent.reverse()

    mempool_rows = []
    for t in mempool:
        first_out = t.outputs[0] if t.outputs else None
        from_addr = ""
        if t.inputs:
            try:
                from_addr = crypto.pubkey_to_address(bytes.fromhex(t.inputs[0].pubkey))
            except Exception:
                from_addr = ""
        mempool_rows.append({
            "txid": t.txid(),
            "from": from_addr,
            "to": first_out.address if first_out else "",
            "amount": first_out.value if first_out else 0,
            "memo": t.memo or "",
        })

    wallet_view = None
    if wallet:
        address = wallet["address"]
        mine = [
            (k, v) for k, v in state.utxos.utxos.items() if v["address"] == address
        ]
        next_height = height + 1
        outputs = []
        mature = 0
        total = 0
        for (txid, vout), v in sorted(mine, key=lambda p: -p[1]["value"]):
            is_mature = not (v["coinbase"] and next_height - v["height"] < COINBASE_MATURITY)
            total += v["value"]
            if is_mature:
                mature += v["value"]
            outputs.append({
                "txid": txid,
                "vout": vout,
                "value": v["value"],
                "height": v["height"],
                "coinbase": v["coinbase"],
                "mature": is_mature,
            })
        wallet_view = {
            "address": address,
            "balance": total,
            "mature": mature,
            "outputs": outputs,
            "handle": _handle_for(address, registry),
        }

    return {
        "empty": False,
        "height": height,
        "tip": state.tip_hash,
        "difficulty": difficulty(state.tip.bits),
        "bits": state.tip.bits,
        "chainwork": state.chainwork,
        "emitted": emitted,
        "circulating": state.circulating(),
        "tx_count": state.tx_count,
        "utxo_count": len(state.utxos.utxos),
        "next_reward": block_subsidy(height + 1),
        "next_retarget": RETARGET_INTERVAL - ((height + 1) % RETARGET_INTERVAL),
        "next_halving": HALVING_INTERVAL - ((height + 1) % HALVING_INTERVAL),
        "miners": miners,
        "balances": balances,
        "blocks": recent,
        "mempool": mempool_rows,
        "wallet": wallet_view,
        "problems": [],
    }


def verify_chain(blocks=None, blocks_path: str | None = None, now: int | None = None) -> dict:
    """Same checks as verify.py. Returns a dict instead of printing."""
    try:
        if blocks is None:
            blocks = chainmod.load_blocks(blocks_path or chainmod.BLOCKS_FILE)
    except ConsensusError as exc:
        return {"ok": False, "error": str(exc)}

    if not blocks:
        return {"ok": False, "error": "chain is empty -- no genesis block"}

    try:
        state = chainmod.replay(blocks, now=now, strict_time=True)
    except ConsensusError as exc:
        return {"ok": False, "error": str(exc)}

    emitted = chainmod.emitted_supply(state.height)
    circulating = state.circulating()
    if emitted != circulating:
        return {
            "ok": False,
            "error": f"supply mismatch: {emitted} emitted but {circulating} unspent",
            "height": state.height,
            "tip": state.tip_hash,
        }

    miners = sorted(state.miners.items(), key=lambda p: (-p[1], p[0]))
    return {
        "ok": True,
        "height": state.height,
        "tip": state.tip_hash,
        "blocks": len(blocks),
        "tx_count": state.tx_count,
        "chainwork": state.chainwork,
        "difficulty": difficulty(state.tip.bits),
        "bits": state.tip.bits,
        "next_bits": state.next_bits(),
        "emitted": emitted,
        "circulating": circulating,
        "utxos": len(state.utxos.utxos),
        "miners": miners,
    }


def fetch_remote(repo: str):
    """Pull blocks, mempool and registry from GitHub. Used by the GUI remote mode."""
    import miner as minermod

    repo = (repo or "").strip()
    if "/" not in repo:
        raise DesktopError("repo must look like owner/name")
    blocks = minermod.load_remote_blocks(repo, bust=True)
    mempool = minermod.load_remote_mempool(repo)
    registry = {}
    try:
        text = minermod.fetch_remote(repo, "chain/registry.json", bust_cache=True)
        registry = json.loads(text) if text.strip() else {}
        if not isinstance(registry, dict):
            registry = {}
    except Exception:
        registry = {}
    return blocks, mempool, registry


# --------------------------------------------------------------------------
# send / identity
# --------------------------------------------------------------------------


def build_send(
    *,
    wallet: dict,
    to: str,
    amount: str,
    fee: str = DEFAULT_FEE,
    memo: str = "",
    blocks=None,
    blocks_path: str | None = None,
    inbox_dir: str = "inbox",
    now: int | None = None,
) -> dict:
    """Sign a transfer and write `inbox/tx-….txt`. Mirrors wallet.py send."""
    if blocks is None:
        blocks = chainmod.load_blocks(blocks_path or chainmod.BLOCKS_FILE)
    if not blocks:
        raise DesktopError("no chain to spend against")

    to = (to or "").strip()
    if not crypto.address_is_valid(to):
        raise DesktopError(f"invalid destination address: {to}")

    memo = memo or ""
    try:
        check_memo(memo)
    except ConsensusError as exc:
        raise DesktopError(str(exc)) from exc

    grains = parse_amount(amount)
    fee_grains = parse_amount(fee)
    if grains <= 0:
        raise DesktopError("amount must be greater than zero")
    if fee_grains < 0:
        raise DesktopError("fee must be non-negative")
    need = grains + fee_grains

    state = chainmod.replay(blocks, now=now, strict_time=False)
    height = state.height + 1

    spendable = []
    for (txid, vout), v in state.utxos.utxos.items():
        if v["address"] != wallet["address"]:
            continue
        if v["coinbase"] and height - v["height"] < COINBASE_MATURITY:
            continue
        spendable.append((txid, vout, v["value"]))
    spendable.sort(key=lambda t: -t[2])

    picked, total = [], 0
    for txid, vout, value in spendable:
        picked.append((txid, vout, value))
        total += value
        if total >= need:
            break
    if total < need:
        raise DesktopError(
            f"insufficient mature funds: have {format_amount(total)}, "
            f"need {format_amount(need)} CRUX"
        )

    outputs = [TxOut(grains, to)]
    change = total - need
    if change > 0:
        outputs.append(TxOut(change, wallet["address"]))

    tx = Tx(
        inputs=[TxIn(txid, vout, wallet["pubkey"], "") for txid, vout, _ in picked],
        outputs=outputs,
        memo=memo,
    )
    digest = tx.sighash()
    priv = crypto.privkey_from_bytes(bytes.fromhex(wallet["privkey"]))
    sig = crypto.sign(priv, digest).hex()
    for i in tx.inputs:
        i.sig = sig

    try:
        validate_tx(tx, state.utxos, height)
    except ConsensusError as exc:
        raise DesktopError(f"refusing to emit an invalid transaction: {exc}") from exc

    line = encode_tx(tx)
    path = write_inbox("tx", tx.txid()[:16], line, inbox_dir=inbox_dir)
    return {
        "txid": tx.txid(),
        "tx": tx,
        "line": line,
        "path": path,
        "amount": grains,
        "fee": fee_grains,
        "change": change,
        "to": to,
        "memo": memo,
        "title": f"crux tx {tx.txid()[:12]}",
    }


def build_identity(
    *,
    wallet: dict,
    handle: str,
    inbox_dir: str = "inbox",
) -> dict:
    handle = (handle or "").strip().lstrip("@")
    try:
        check_miner_name(handle)
    except ConsensusError as exc:
        raise DesktopError(str(exc)) from exc

    priv = crypto.privkey_from_bytes(bytes.fromhex(wallet["privkey"]))
    digest = crypto.sha256d(b"crux-identity-v1|" + handle.encode())
    sig = crypto.sign(priv, digest).hex()
    line = f"{ID_PREFIX}{handle}:{wallet['pubkey']}:{sig}"
    path = write_inbox("id", handle, line, inbox_dir=inbox_dir)
    return {
        "line": line,
        "path": path,
        "handle": handle,
        "address": wallet["address"],
        "pubkey": wallet["pubkey"],
        "sig": sig,
        "title": f"crux identity {handle}",
    }


def verify_identity_line(line: str, wallet: dict | None = None) -> bool:
    """True if `line` is a well-formed identity payload whose signature checks."""
    if not line.startswith(ID_PREFIX):
        return False
    payload = line[len(ID_PREFIX):]
    parts = payload.split(":")
    if len(parts) != 3:
        return False
    handle, pubkey, sig_hex = parts
    try:
        check_miner_name(handle)
        digest = crypto.sha256d(b"crux-identity-v1|" + handle.encode())
        ok = crypto.verify(bytes.fromhex(pubkey), digest, bytes.fromhex(sig_hex))
    except Exception:
        return False
    if wallet and pubkey != wallet.get("pubkey"):
        return False
    return bool(ok)


# --------------------------------------------------------------------------
# mining
# --------------------------------------------------------------------------


def validate_miner_fields(handle: str, message: str) -> tuple[str, str]:
    handle = (handle or "").strip().lstrip("@")
    message = message or ""
    try:
        check_miner_name(handle)
        check_message(message, is_genesis=False)
    except ConsensusError as exc:
        raise DesktopError(str(exc)) from exc
    return handle, message


def mine_block(
    blocks,
    miner_name: str,
    address: str,
    message: str = "",
    mempool=None,
    now: int | None = None,
    stop=None,
    on_progress=None,
    quiet: bool = True,
    inbox_dir: str = "inbox",
    write: bool = True,
):
    """
    Mine one block. Returns a dict with the block and submission line, or
    None if `stop` aborted the loop.
    """
    import miner as minermod

    miner_name, message = validate_miner_fields(miner_name, message)
    if not crypto.address_is_valid(address):
        raise DesktopError(f"invalid payout address: {address}")

    block = minermod.mine_one(
        blocks,
        miner_name,
        address,
        message,
        mempool or [],
        now=now,
        stop=stop,
        on_progress=on_progress,
        quiet=quiet,
    )
    if block is None:
        return None
    line = encode_block(block)
    path = ""
    if write:
        path = write_inbox("block", block.block_hash()[:16], line, inbox_dir=inbox_dir)
    return {
        "block": block,
        "line": line,
        "path": path,
        "hash": block.block_hash(),
        "height": block.height,
        "title": f"crux block {block.height} {block.block_hash()[:12]}",
    }


def submit_block(repo: str, block, line: str) -> bool:
    import miner as minermod

    repo = (repo or "").strip()
    if "/" not in repo:
        raise DesktopError("repo must look like owner/name")
    return minermod.submit_issue(repo, block, line)
