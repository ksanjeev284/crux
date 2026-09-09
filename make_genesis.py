#!/usr/bin/env python3
"""
Mine the CRUX genesis block. Run once, before anything is pushed.

    python3 make_genesis.py --miner YOUR_HANDLE --message "..." --address crux1...

The genesis block is immutable: its hash is the root every later block
chains back to. Change the message or the payout address after the chain
has started and every block after it becomes invalid.
"""

import argparse
import os
import sys
import time

from crux import chain as chainmod
from crux import crypto, render
from crux import pow as powfn
from crux.consensus import (
    GENESIS_BITS,
    Block,
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
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--miner", required=True)
    ap.add_argument("--message", required=True, help="goes in the coinbase, max 256 bytes at genesis")
    ap.add_argument("--address", required=True, help="receives the genesis reward")
    ap.add_argument("--timestamp", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    check_miner_name(args.miner)
    check_message(args.message, is_genesis=True)
    if not crypto.address_is_valid(args.address):
        raise SystemExit(f"invalid address: {args.address}")

    if os.path.exists(chainmod.BLOCKS_FILE) and not args.force:
        raise SystemExit(
            f"{chainmod.BLOCKS_FILE} already exists. Refusing to re-mine genesis.\n"
            f"Use --force only if the chain has never been published."
        )

    coinbase = Tx(coinbase=args.message, cb_height=0, outputs=[TxOut(block_subsidy(0), args.address)])
    root = merkle_root([coinbase.txid()])
    ts = args.timestamp or int(time.time())

    block = Block(
        height=0,
        prev_hash="00" * 32,
        merkle_root=root,
        timestamp=ts,
        bits=GENESIS_BITS,
        miner=args.miner,
        txs=[coinbase],
    )

    target = bits_to_target(GENESIS_BITS)
    print(f"mining CRUX genesis at difficulty {difficulty(GENESIS_BITS):,.1f}")
    print(f"  work     {target_to_work(target):,} expected hashes")
    print(f"  puzzle   1 x subset-sum(n={powfn.N}) under a hash target")
    print(f'  message  "{args.message}"')
    print(f"  reward   {format_amount(block_subsidy(0))} CRUX -> {args.address}")
    sys.stdout.flush()

    started = time.time()
    nonce = 0
    attempts = 0
    while True:
        block.nonce = nonce
        core = block.header_core()
        numbers, tgt = powfn.instance(core)
        subset = powfn.solve_instance(numbers, tgt)
        attempts += 1
        if subset is not None:
            block.solution = powfn.encode_solution(subset)
            if powfn.hash_meets_target(block.digest(), target):
                break
        nonce += 1
        if attempts % 1 == 0:
            elapsed = max(0.001, time.time() - started)
            sys.stderr.write(
                f"\r  nonce {nonce:,}  {attempts / elapsed:.2f} puzzles/s  {elapsed:.0f}s…   "
            )
            sys.stderr.flush()

    sys.stderr.write("\r" + " " * 72 + "\r")
    elapsed = time.time() - started

    if os.path.exists(chainmod.BLOCKS_FILE):
        os.remove(chainmod.BLOCKS_FILE)
    chainmod.append_block(block)

    state = chainmod.load_state()
    if os.path.exists("README.md"):
        render.update_readme(state)
    render.render_svg(state)

    print(f"  found in {elapsed:.1f}s after {attempts} puzzle(s) "
          f"({attempts / max(elapsed, 0.001):.2f}/s)")
    print()
    print(f"  genesis hash  {block.block_hash()}")
    print(f"  nonce         {nonce:,}")
    print(f"  timestamp     {ts}")
    print()
    print("written to chain/blocks.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
