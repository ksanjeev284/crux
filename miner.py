#!/usr/bin/env python3
"""
CRUX miner. Standard library only -- no pip install.

    python3 miner.py --miner YOUR_GITHUB_HANDLE --message "gm"

Mining happens on your machine. Each attempt is one subset-sum puzzle;
the block is valid when that puzzle is solved *and* the header hashes at
or below the current target. The proof is always 8 bytes, at any difficulty.

The miner writes `inbox/block-<hash>.txt` and prints a `crux-block-v1:`
line. Submit it as a **new GitHub issue** (or a pull request adding that
file). Do not comment on an old thread.

    python3 miner.py --submit          # open the issue with `gh` after each block
    python3 miner.py --once            # one block and exit
    python3 miner.py --cuda            # solve knapsacks on an NVIDIA GPU
    python3 miner.py --cpu             # force the Python solver
    python3 miner.py --help
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from crux import chain as chainmod
from crux import crypto
from crux import pow as powfn
from crux.consensus import (
    Block,
    ConsensusError,
    MAX_TXS_PER_BLOCK,
    Tx,
    TxOut,
    bits_to_target,
    block_subsidy,
    check_message,
    check_miner_name,
    difficulty,
    format_amount,
    merkle_root,
    target_to_work,
    validate_tx,
)
from crux.wire import encode_block

RAW_BASE = "https://raw.githubusercontent.com/{repo}/main/"
WALLET = "crux-wallet.json"

_GITHUB_TOKEN = None


def get_github_token() -> str:
    global _GITHUB_TOKEN
    if _GITHUB_TOKEN is not None:
        return _GITHUB_TOKEN
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        _GITHUB_TOKEN = token.strip()
        return _GITHUB_TOKEN
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            _GITHUB_TOKEN = proc.stdout.strip()
            return _GITHUB_TOKEN
    except Exception:
        pass
    _GITHUB_TOKEN = ""
    return _GITHUB_TOKEN


def fetch_remote(repo: str, path: str, bust_cache: bool = False) -> str:
    if bust_cache:
        token = get_github_token()
        api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {
            "User-Agent": "CRUX-Miner",
            "Accept": "application/vnd.github.v3.raw",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(api_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise SystemExit(f"could not fetch {api_url}: 404 Not Found")
        except Exception:
            pass

    url = RAW_BASE.format(repo=repo) + path
    if bust_cache:
        url += f"?t={int(time.time() * 1000)}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "CRUX-Miner", "Cache-Control": "no-cache", "Pragma": "no-cache"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"could not fetch {url}: {exc}")


def load_remote_blocks(repo: str, bust: bool = False):
    text = fetch_remote(repo, "chain/blocks.jsonl", bust_cache=bust)
    blocks = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            blocks.append(Block.from_dict(json.loads(line)))
    return blocks


def load_remote_mempool(repo: str):
    try:
        text = fetch_remote(repo, "chain/mempool.jsonl", bust_cache=True)
    except SystemExit:
        return []
    txs = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            txs.append(Tx.from_dict(json.loads(line)))
    return txs


def load_wallet(path: str):
    if not os.path.exists(path):
        raise SystemExit(f"no wallet at {path}. run:  python3 wallet.py new")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def pick_txs(mempool, utxos, height):
    """Highest-fee first, skipping anything that doesn't apply cleanly."""
    ranked = []
    for t in mempool:
        try:
            fee = validate_tx(t, utxos, height)
        except ConsensusError:
            continue
        ranked.append((fee, t))
    ranked.sort(key=lambda p: -p[0])

    chosen = []
    working = utxos.copy()
    # leave room for the coinbase
    for fee, t in ranked:
        if 1 + len(chosen) >= MAX_TXS_PER_BLOCK:
            break
        try:
            validate_tx(t, working, height)
        except ConsensusError:
            continue
        for i in t.inputs:
            working.spend(i.txid, i.vout)
        working.add_tx(t, height)
        chosen.append(t)
    return chosen


def fees_of(txs, utxos, height) -> int:
    working = utxos.copy()
    total = 0
    for t in txs:
        total += validate_tx(t, working, height)
        for i in t.inputs:
            working.spend(i.txid, i.vout)
        working.add_tx(t, height)
    return total


def write_inbox(block: Block) -> str:
    os.makedirs("inbox", exist_ok=True)
    path = os.path.join("inbox", f"block-{block.block_hash()[:16]}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(encode_block(block) + "\n")
    return path


def submit_issue(repo: str, block: Block, line: str) -> bool:
    """Open a *new* issue. Never comments on an existing thread."""
    title = f"crux block {block.height} {block.block_hash()[:12]}"
    try:
        proc = subprocess.run(
            [
                "gh", "issue", "create",
                "--repo", repo,
                "--title", title,
                "--label", "crux",
                "--body", line,
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            print(f"  submitted {proc.stdout.strip()}")
            return True
        sys.stderr.write(f"  gh issue create failed: {proc.stderr.strip()}\n")
        return False
    except FileNotFoundError:
        sys.stderr.write("  `gh` not found; install GitHub CLI or paste the line by hand\n")
        return False


def mine_one(blocks, miner, address, message, mempool, now=None,
             stop=None, on_progress=None, quiet=False, use_cuda=None):
    """
    Mine one block extending `blocks`.

    `stop` is an optional callable; when it returns True the loop exits and
    this returns None. `on_progress` is an optional callback receiving a dict
    of nonce/attempts/solved/elapsed/rate. Both exist so a GUI can run this
    on a worker thread without changing the work function.
    """
    def say(msg=""):
        if not quiet:
            print(msg)

    state = chainmod.replay(blocks, strict_time=False)
    height = len(blocks)
    bits = chainmod.bits_for_height(height, blocks)
    target = bits_to_target(bits)
    ts = now if now is not None else int(time.time())
    mtp = state.median_time_past()
    if ts <= mtp:
        ts = mtp + 1

    txs = pick_txs(mempool, state.utxos, height)
    fee = fees_of(txs, state.utxos, height) if txs else 0
    coinbase = Tx(
        coinbase=message,
        cb_height=height,
        outputs=[TxOut(block_subsidy(height) + fee, address)],
    )
    all_txs = [coinbase] + txs
    block = Block(
        height=height,
        prev_hash=state.tip_hash,
        merkle_root=merkle_root([t.txid() for t in all_txs]),
        timestamp=ts,
        bits=bits,
        miner=miner,
        txs=all_txs,
    )

    solver = powfn.cuda_info() if (use_cuda is not False) else "cpu"
    if use_cuda is False:
        powfn.set_cuda(False)
    elif use_cuda is True:
        powfn.set_cuda(True)
        solver = powfn.cuda_info()
    say(f"mining height {height}  difficulty {difficulty(bits):,.1f}  "
        f"bits {bits:#010x}  work {target_to_work(target):,}  {solver}")
    say(f"  tip     {state.tip_hash[:20]}…" if height else "  genesis")
    say(f"  reward  {format_amount(block_subsidy(height) + fee)} CRUX"
        f"{f' (fees {format_amount(fee)})' if fee else ''}"
        f"  txs {len(all_txs)}")

    started = time.time()
    nonce = 0
    attempts = 0
    solved = 0
    last_report = started
    while True:
        if stop is not None and stop():
            elapsed = max(0.001, time.time() - started)
            if on_progress is not None:
                on_progress({
                    "event": "stopped",
                    "height": height,
                    "nonce": nonce,
                    "attempts": attempts,
                    "solved": solved,
                    "elapsed": elapsed,
                    "rate": attempts / elapsed,
                })
            say("  stopped")
            return None
        block.nonce = nonce
        core = block.header_core()
        numbers, tgt = powfn.instance(core)
        subset = powfn.solve_instance(numbers, tgt, use_cuda=use_cuda)
        attempts += 1
        if subset is not None:
            solved += 1
            block.solution = powfn.encode_solution(subset)
            digest = block.digest()
            if powfn.hash_meets_target(digest, target):
                elapsed = max(0.001, time.time() - started)
                say(
                    f"  found  {block.block_hash()}  nonce {nonce}  "
                    f"{attempts} puzzle(s) in {elapsed:.1f}s  "
                    f"({attempts / elapsed:.2f}/s)"
                )
                if on_progress is not None:
                    on_progress({
                        "event": "found",
                        "height": height,
                        "nonce": nonce,
                        "attempts": attempts,
                        "solved": solved,
                        "elapsed": elapsed,
                        "rate": attempts / elapsed,
                        "hash": block.block_hash(),
                    })
                return block
        nonce += 1
        now_t = time.time()
        if now_t - last_report >= 2:
            elapsed = max(0.001, now_t - started)
            if not quiet:
                sys.stderr.write(
                    f"\r  nonce {nonce:,}  {attempts / elapsed:.2f} puzzles/s  "
                    f"{solved} solved  {elapsed:.0f}s   "
                )
                sys.stderr.flush()
            if on_progress is not None:
                on_progress({
                    "event": "progress",
                    "height": height,
                    "nonce": nonce,
                    "attempts": attempts,
                    "solved": solved,
                    "elapsed": elapsed,
                    "rate": attempts / elapsed,
                })
            last_report = now_t


def wait_for_height(repo: str, height: int, timeout: int = 180) -> list:
    """Poll the remote chain until `height` is present or we time out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            blocks = load_remote_blocks(repo, bust=True)
        except SystemExit:
            time.sleep(8)
            continue
        if len(blocks) > height:
            return blocks
        time.sleep(8)
    return load_remote_blocks(repo, bust=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="CRUX miner")
    ap.add_argument("--miner", required=True, help="your GitHub handle; sealed into the header")
    ap.add_argument("--message", default="", help="coinbase message, max 80 bytes")
    ap.add_argument("--wallet", default=WALLET)
    ap.add_argument("--address", default="", help="payout address (default: wallet)")
    ap.add_argument("--repo", default="", help="owner/name to follow and submit to")
    ap.add_argument("--once", action="store_true", help="mine one block and exit")
    ap.add_argument("--submit", action="store_true",
                    help="open a new GitHub issue with `gh` after each block")
    ap.add_argument("--local", action="store_true",
                    help="mine against the local chain/blocks.jsonl only")
    accel = ap.add_mutually_exclusive_group()
    accel.add_argument("--cuda", action="store_true",
                       help="solve knapsacks on the GPU (Numba CUDA or nvcc)")
    accel.add_argument("--cpu", action="store_true",
                       help="force the Python meet-in-the-middle solver")
    args = ap.parse_args()

    if args.cpu:
        powfn.set_cuda(False)
    elif args.cuda:
        powfn.set_cuda(True)
        if "cpu" == powfn.cuda_info():
            raise SystemExit("CUDA was requested but no GPU solver is available")

    check_miner_name(args.miner)
    check_message(args.message, is_genesis=False)

    if args.address:
        address = args.address
        if not crypto.address_is_valid(address):
            raise SystemExit(f"invalid address: {address}")
    else:
        address = load_wallet(args.wallet)["address"]

    repo = args.repo.strip()
    use_remote = bool(repo) and not args.local

    if use_remote:
        blocks = load_remote_blocks(repo, bust=True)
        mempool = load_remote_mempool(repo)
        print(f"following {repo} at height {len(blocks) - 1}")
    else:
        blocks = chainmod.load_blocks()
        if not blocks:
            raise SystemExit("no local chain. run make_genesis.py or pass --repo owner/name")
        mempool = []
        path = os.path.join("chain", "mempool.jsonl")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                mempool = [Tx.from_dict(json.loads(l)) for l in fh if l.strip()]
        print(f"local chain at height {len(blocks) - 1}")

    while True:
        block = mine_one(blocks, args.miner, address, args.message, mempool)
        line = encode_block(block)
        path = write_inbox(block)
        print()
        print(f"wrote {path}")
        print("Open a new GitHub issue whose body is this line, or a pull request")
        print("that adds only that file. Do not comment on an old issue.")
        print()
        print(line)
        print()

        if args.submit:
            if not repo:
                print("  --submit needs --repo owner/name")
            else:
                submit_issue(repo, block, line)

        if args.once:
            return 0

        if use_remote:
            print(f"waiting for height {block.height} to land on {repo}…")
            blocks = wait_for_height(repo, block.height)
            mempool = load_remote_mempool(repo)
            if not any(b.block_hash() == block.block_hash() for b in blocks):
                print("  our block did not land (lost the race); mining on the new tip")
        else:
            # local demo loop: append so the next iteration has a new tip
            chainmod.append_block(block)
            blocks.append(block)
            mempool = [t for t in mempool if t.txid() not in {x.txid() for x in block.txs}]
        print()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        raise SystemExit(0)
