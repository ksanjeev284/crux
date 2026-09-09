#!/usr/bin/env python3
"""
Process one CRUX submission.

Called by .github/workflows/. Reads the body and the author, validates
whatever it finds, updates the chain or the mempool, and writes a reply
to stdout (and to $GITHUB_OUTPUT as `reply`).

Submissions arrive as:
  - a newly opened issue whose body contains a crux-*-v1: line, or
  - a pull request that adds exactly one file under inbox/

Never as a comment on a standing thread. Each submission is one document,
capped at 24 KB, so GitHub's 64 KB issue limit and Actions payload limits
are not in play even after years of blocks.

Exit code is always 0: a rejected submission is a normal outcome that
deserves an explanatory reply, not a red X on the workflow.
"""

import json
import os
import time

from crux import chain as chainmod
from crux import crypto
from crux import render
from crux.consensus import (
    MAX_SUBMISSION_BYTES,
    Block,
    ConsensusError,
    Tx,
    UTXOSet,
    check_miner_name,
    difficulty,
    format_amount,
    validate_block,
    validate_tx,
)
from crux.wire import find_payload, decode_json

MEMPOOL = os.path.join("chain", "mempool.jsonl")
REGISTRY = os.path.join("chain", "registry.json")
MAX_MEMPOOL = 64


def emit(reply: str, changed: bool) -> None:
    print(reply)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            delim = "CRUX_EOF_%d" % int(time.time())
            fh.write(f"reply<<{delim}\n{reply}\n{delim}\n")
            fh.write(f"changed={'true' if changed else 'false'}\n")


def load_mempool():
    if not os.path.exists(MEMPOOL):
        return []
    with open(MEMPOOL, encoding="utf-8") as fh:
        return [Tx.from_dict(json.loads(l)) for l in fh if l.strip()]


def save_mempool(txs) -> None:
    os.makedirs(os.path.dirname(MEMPOOL), exist_ok=True)
    with open(MEMPOOL, "w", encoding="utf-8") as fh:
        for t in txs:
            fh.write(json.dumps(t.to_dict(), separators=(",", ":"), sort_keys=True) + "\n")


def handle_identity(payload: str, author: str) -> tuple[str, bool]:
    """
    Bind a GitHub handle to a CRUX address.

    Two proofs are required and neither alone is enough: the signature shows
    control of the key, and posting from the account shows control of the
    handle. This is not consensus -- it only decides whose name is shown
    beside a balance, and verify.py ignores it entirely.
    """
    parts = payload.split(":")
    if len(parts) != 3:
        return "**Rejected.** Malformed identity line. Run `python3 wallet.py identity`.", False
    handle, pubkey, sig = parts

    try:
        check_miner_name(handle)
    except ConsensusError:
        return "**Rejected.** Handle must be 1-39 characters of letters, digits, or hyphen.", False

    if handle.lower() != author.lower():
        return (
            f"**Rejected.** That line claims the handle `{handle}` but was posted "
            f"by @{author}. Re-run with `--handle {author}`.",
            False,
        )
    try:
        pub_bytes = bytes.fromhex(pubkey)
        sig_bytes = bytes.fromhex(sig)
        address = crypto.pubkey_to_address(pub_bytes)
    except ValueError:
        return "**Rejected.** Malformed public key or signature.", False

    digest = crypto.sha256d(b"crux-identity-v1|" + handle.encode())
    if not crypto.verify(pub_bytes, digest, sig_bytes):
        return "**Rejected.** That signature does not verify for this handle.", False

    registry = {}
    if os.path.exists(REGISTRY):
        with open(REGISTRY, encoding="utf-8") as fh:
            registry = json.load(fh)
    previous = registry.get(handle, {}).get("address")
    registry[handle] = {"address": address, "pubkey": pubkey}
    os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
    with open(REGISTRY, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2, sort_keys=True)

    state = chainmod.load_state()
    render.update_readme(state)

    note = f"\n\nThis replaces your previous address `{previous}`." if previous else ""
    return (
        f"### Identity registered\n\n"
        f"| | |\n|---|---|\n"
        f"| handle | @{handle} |\n| address | `{address}` |\n\n"
        f"Your name will now appear beside your balance in the ledger.{note}",
        True,
    )


def handle_block(data, author: str) -> tuple[str, bool]:
    block = Block.from_dict(data)

    if block.miner.lower() != author.lower():
        return (
            f"**Rejected.** This block names `{block.miner}` as the miner but was "
            f"submitted by @{author}. The miner handle is inside the hashed header, "
            f"so it cannot be changed without redoing the work.\n\n"
            f"Re-mine with `--miner {author}`.",
            False,
        )

    blocks = chainmod.load_blocks()
    state = chainmod.replay(blocks)
    expected_bits = state.next_bits()

    try:
        validate_block(
            block,
            state.tip,
            state.utxos,
            expected_bits,
            state.median_time_past(),
            int(time.time()),
        )
    except ConsensusError as exc:
        return (
            f"**Rejected.** {exc}\n\n"
            f"Current tip is `{state.tip_hash}` at height `{state.height}`, "
            f"next block needs bits `{expected_bits:#010x}`.",
            False,
        )

    chainmod.append_block(block)

    included = {t.txid() for t in block.txs[1:]}
    if included:
        save_mempool([t for t in load_mempool() if t.txid() not in included])

    new_state = chainmod.load_state()
    render.update_readme(new_state)
    render.render_svg(new_state)

    reward = format_amount(sum(o.value for o in block.txs[0].outputs))
    msg = block.txs[0].coinbase
    lines = [
        f"### Block `{block.height}` accepted",
        "",
        f"| | |",
        f"|---|---|",
        f"| hash | `{block.block_hash()}` |",
        f"| miner | @{block.miner} |",
        f"| nonce | `{block.nonce}` |",
        f"| difficulty | `{difficulty(block.bits):,.1f}` |",
        f"| reward | `{reward} CRUX` |",
        f"| transactions | `{len(block.txs)}` |",
    ]
    if msg:
        lines.append(f"| message | `{msg}` |")
    lines += [
        "",
        f"Chain is now at height `{new_state.height}`, next difficulty "
        f"`{difficulty(new_state.next_bits()):,.1f}`.",
        "",
        f"The README has been updated. Verify the whole chain with `python3 verify.py`.",
    ]
    return "\n".join(lines), True


def handle_tx(data, author: str) -> tuple[str, bool]:
    tx = Tx.from_dict(data)
    if tx.is_coinbase:
        return "**Rejected.** Coinbase transactions are created by miners, not submitted.", False

    state = chainmod.load_state()
    mempool = load_mempool()

    if len(mempool) >= MAX_MEMPOOL:
        return f"**Rejected.** Mempool is full ({MAX_MEMPOOL}). Mine a block to drain it.", False
    if any(t.txid() == tx.txid() for t in mempool):
        return f"Already in the mempool as `{tx.txid()[:20]}…`.", False

    # Validate against the chain tip plus everything already queued, so two
    # queued transactions cannot spend the same output.
    working: UTXOSet = state.utxos.copy()
    height = state.height + 1
    for t in mempool:
        try:
            validate_tx(t, working, height)
        except ConsensusError:
            continue
        for i in t.inputs:
            working.spend(i.txid, i.vout)
        working.add_tx(t, height)

    try:
        fee = validate_tx(tx, working, height)
    except ConsensusError as exc:
        return f"**Rejected.** {exc}", False

    mempool.append(tx)
    save_mempool(mempool)

    total = sum(o.value for o in tx.outputs)
    return (
        "\n".join(
            [
                f"### Transaction queued",
                "",
                f"| | |",
                f"|---|---|",
                f"| txid | `{tx.txid()}` |",
                f"| sending | `{format_amount(total)} CRUX` across {len(tx.outputs)} output(s) |",
                f"| fee | `{format_amount(fee)} CRUX` |",
                f"| mempool | `{len(mempool)}` transaction(s) waiting |",
                "",
                "It will be included by whoever mines the next block. "
                "Higher fees get picked first.",
            ]
        ),
        True,
    )


def main() -> int:
    body = os.environ.get("COMMENT_BODY", "")
    author = os.environ.get("COMMENT_AUTHOR", "")

    if not author:
        emit("**Rejected.** No author supplied.", False)
        return 0

    if len(body.encode("utf-8")) > MAX_SUBMISSION_BYTES:
        emit(
            f"**Rejected.** Submission is {len(body.encode('utf-8'))} bytes; "
            f"the cap is {MAX_SUBMISSION_BYTES} so GitHub issue bodies (64 KB) "
            "and Actions never overflow. A CRUX block is a few kilobytes even "
            "at the highest difficulty — strip any extra text and resubmit as "
            "a new issue or an `inbox/` pull request.",
            False,
        )
        return 0

    kind, payload = find_payload(body)
    if not kind:
        emit(
            "I could not find a submission in that body.\n\n"
            "Blocks start with `crux-block-v1:`, transactions with `crux-tx-v1:` and "
            "identity registrations with `crux-id-v1:`, each on its own line. "
            "See the README for how to produce one. Open a **new** issue or an "
            "`inbox/` pull request — do not comment on an old thread.",
            False,
        )
        return 0

    if kind == "id":
        try:
            reply, changed = handle_identity(payload, author)
        except Exception as exc:  # noqa: BLE001
            reply, changed = f"**Rejected.** `{type(exc).__name__}: {exc}`", False
        emit(reply, changed)
        return 0

    try:
        data = decode_json(payload)
    except Exception as exc:  # noqa: BLE001 - any malformed paste lands here
        emit(f"**Rejected.** Could not decode that {kind}: `{exc}`", False)
        return 0

    if not isinstance(data, dict):
        emit(f"**Rejected.** That {kind} payload is not an object.", False)
        return 0

    try:
        reply, changed = handle_block(data, author) if kind == "block" else handle_tx(data, author)
    except ConsensusError as exc:
        reply, changed = f"**Rejected.** {exc}", False
    except Exception as exc:  # noqa: BLE001
        reply, changed = f"**Rejected.** Malformed {kind}: `{type(exc).__name__}: {exc}`", False

    emit(reply, changed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
