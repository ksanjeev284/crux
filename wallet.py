#!/usr/bin/env python3
"""
CRUX wallet. Standard library only.

    python3 wallet.py new                       create a key pair
    python3 wallet.py address                   show your address
    python3 wallet.py balance                   show confirmed balance
    python3 wallet.py send --to ADDR --amount 1.5 --fee 0.001
    python3 wallet.py identity --handle YOUR_GITHUB_HANDLE

`send` prints a signed transaction and writes it under inbox/. Open a pull
request with that file, or open a new GitHub issue containing the line —
never a comment on a long-lived thread.

The private key is stored in plain text in crux-wallet.json. CRUX coins are
worth nothing and this key must never be reused anywhere that matters.
"""

import argparse
import json
import os
import secrets
import stat
import sys

from crux import chain as chainmod
from crux import crypto
from crux.consensus import COIN, COINBASE_MATURITY, ConsensusError, Tx, TxIn, TxOut, format_amount, validate_tx
from crux.wire import ID_PREFIX, encode_tx

WALLET = "crux-wallet.json"


def load_wallet(path=WALLET):
    if not os.path.exists(path):
        raise SystemExit(f"no wallet at {path}. run:  python3 wallet.py new")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_new(args):
    if os.path.exists(args.wallet) and not args.force:
        raise SystemExit(f"{args.wallet} already exists. use --force to overwrite it.")
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
    with open(args.wallet, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(args.wallet, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    print(f"wallet written to {args.wallet}")
    print(f"address  {address}")


def cmd_address(args):
    print(load_wallet(args.wallet)["address"])


def _state(args):
    return chainmod.replay(chainmod.load_blocks(), strict_time=False)


def cmd_balance(args):
    w = load_wallet(args.wallet)
    state = _state(args)
    mine = [
        (k, v) for k, v in state.utxos.utxos.items() if v["address"] == w["address"]
    ]
    total = sum(v["value"] for _, v in mine)
    print(f"address  {w['address']}")
    print(f"balance  {format_amount(total)} CRUX across {len(mine)} output(s)")
    for (txid, vout), v in sorted(mine, key=lambda p: -p[1]["value"]):
        tag = " (coinbase)" if v["coinbase"] else ""
        print(f"  {txid[:16]}…:{vout}  {format_amount(v['value']):>18} CRUX  "
              f"height {v['height']}{tag}")


def parse_amount(s: str) -> int:
    """Convert a decimal CRUX string into an integer number of grains."""
    if "." not in s:
        return int(s) * COIN
    whole, frac = s.split(".", 1)
    if len(frac) > 8:
        raise SystemExit("amounts have at most 8 decimal places")
    return int(whole or 0) * COIN + int(frac.ljust(8, "0"))


def _write_inbox(kind: str, name: str, line: str) -> str:
    os.makedirs("inbox", exist_ok=True)
    path = os.path.join("inbox", f"{kind}-{name}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path


def cmd_send(args):
    w = load_wallet(args.wallet)
    state = _state(args)
    height = state.height + 1

    if not crypto.address_is_valid(args.to):
        raise SystemExit(f"invalid destination address: {args.to}")

    amount = parse_amount(args.amount)
    fee = parse_amount(args.fee)
    need = amount + fee

    spendable = []
    for (txid, vout), v in state.utxos.utxos.items():
        if v["address"] != w["address"]:
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
        raise SystemExit(
            f"insufficient mature funds: have {format_amount(total)}, "
            f"need {format_amount(need)} CRUX"
        )

    outputs = [TxOut(amount, args.to)]
    change = total - need
    if change > 0:
        outputs.append(TxOut(change, w["address"]))

    tx = Tx(
        inputs=[TxIn(txid, vout, w["pubkey"], "") for txid, vout, _ in picked],
        outputs=outputs,
        memo=args.memo,
    )
    digest = tx.sighash()
    priv = crypto.privkey_from_bytes(bytes.fromhex(w["privkey"]))
    sig = crypto.sign(priv, digest).hex()
    for i in tx.inputs:
        i.sig = sig

    try:
        validate_tx(tx, state.utxos, height)
    except ConsensusError as exc:
        raise SystemExit(f"refusing to emit an invalid transaction: {exc}")

    line = encode_tx(tx)
    path = _write_inbox("tx", tx.txid()[:16], line)

    print(f"txid    {tx.txid()}")
    print(f"send    {format_amount(amount)} CRUX -> {args.to}")
    print(f"fee     {format_amount(fee)} CRUX")
    if args.memo:
        print(f'memo    "{args.memo}"')
    if change:
        print(f"change  {format_amount(change)} CRUX -> {w['address']}")
    print()
    print(f"wrote {path}")
    print("Open a pull request adding only that file, or open a *new* GitHub")
    print("issue whose body is the line below. Do not comment on an old issue.")
    print()
    print(line)


def cmd_identity(args):
    """
    Prove that one GitHub handle and one CRUX address are the same person.

    The signature proves you hold the key; posting it from your account
    proves you hold the handle. Both directions are needed, and neither is
    consensus -- it only decides whose name appears next to a balance.
    """
    w = load_wallet(args.wallet)
    priv = crypto.privkey_from_bytes(bytes.fromhex(w["privkey"]))
    digest = crypto.sha256d(b"crux-identity-v1|" + args.handle.encode())
    sig = crypto.sign(priv, digest).hex()
    line = f"{ID_PREFIX}{args.handle}:{w['pubkey']}:{sig}"
    path = _write_inbox("id", args.handle, line)
    print(f"address  {w['address']}")
    print(f"handle   {args.handle}")
    print()
    print(f"wrote {path}")
    print("Open a new issue (or a pull request with that file) *from the")
    print("GitHub account it names*. Do not comment on an old issue.")
    print()
    print(line)


def main():
    ap = argparse.ArgumentParser(description="CRUX wallet")
    ap.add_argument("--wallet", default=WALLET)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="generate a new key pair")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_new)

    sub.add_parser("address", help="print your address").set_defaults(func=cmd_address)
    sub.add_parser("balance", help="print your balance").set_defaults(func=cmd_balance)

    p = sub.add_parser("identity", help="link your GitHub handle to your address")
    p.add_argument("--handle", required=True, help="your GitHub username")
    p.set_defaults(func=cmd_identity)

    p = sub.add_parser("send", help="build and sign a transaction")
    p.add_argument("--to", required=True)
    p.add_argument("--amount", required=True, help="in CRUX, e.g. 1.5")
    p.add_argument("--fee", default="0.001", help="in CRUX")
    p.add_argument("--memo", default="", help="a note to attach, max 120 bytes")
    p.set_defaults(func=cmd_send)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
