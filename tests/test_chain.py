#!/usr/bin/env python3
"""
End-to-end consensus tests for CRUX.

Runs at the chain's easiest allowed difficulty so the whole suite takes a
few seconds. Exercises a real spend with real signatures, then tries to
break the chain nine different ways and asserts each attempt is rejected.

    python3 tests/test_chain.py
"""

import copy
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crux import chain as chainmod  # noqa: E402
from crux import consensus as k  # noqa: E402
from crux import crypto  # noqa: E402
from crux import pow as rp  # noqa: E402

# Shrink the puzzle so the suite runs in seconds. The live chain uses n=44;
# n=16 exercises exactly the same code paths at 2^8 instead of 2^20 work.
rp.N = 16
rp.B = rp.N - 2
# Floor and genesis are the easiest compact target: every hash passes, so
# mining is "find any solvable knapsack".
k.POW_LIMIT_BITS = 0x2100FFFF
k.GENESIS_BITS = k.POW_LIMIT_BITS

PASSED = []
BASE_TIME = 1_750_000_000


def ok(label):
    PASSED.append(label)
    print(f"  ok    {label}")


def expect_reject(label, fn):
    """Assert that `fn` raises ConsensusError, and show the message."""
    try:
        fn()
    except k.ConsensusError as exc:
        msg = str(exc)
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


def mine_block(blocks, miner, address, txs=None, message="", timestamp=None):
    """Build and mine a valid block extending `blocks`."""
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
                break
        nonce += 1
        if nonce >= rp.MAX_NONCE:
            raise RuntimeError("could not mine a test block")
    return block


def sign_tx(tx, priv):
    digest = tx.sighash()
    sig = crypto.sign(priv, digest).hex()
    for i in tx.inputs:
        i.sig = sig
    return tx


def run():
    now = BASE_TIME + 200 * 600
    print("CRUX consensus tests")
    print()

    alice_priv, alice_pub, alice_addr = new_key()
    bob_priv, bob_pub, bob_addr = new_key()

    # ---- build a chain -------------------------------------------------
    blocks = [mine_block([], "crux", alice_addr, message="CRUX genesis")]
    ok("genesis mined and self-consistent")

    for _ in range(1, 12):
        blocks.append(mine_block(blocks, "crux", alice_addr))
    state = chainmod.replay(blocks, now=now)
    assert state.height == 11
    ok(f"12 blocks replay clean (height {state.height})")

    # ---- a real spend --------------------------------------------------
    gen_coinbase_txid = blocks[0].txs[0].txid()
    entry = state.utxos.get(gen_coinbase_txid, 0)
    assert entry and entry["address"] == alice_addr

    amount = 10 * k.COIN
    fee = 5000
    change = entry["value"] - amount - fee
    spend = k.Tx(
        inputs=[k.TxIn(gen_coinbase_txid, 0, alice_pub, "")],
        outputs=[k.TxOut(amount, bob_addr), k.TxOut(change, alice_addr)],
    )
    sign_tx(spend, alice_priv)

    blocks.append(mine_block(blocks, "octocat", bob_addr, txs=[spend], message="first spend"))
    state = chainmod.replay(blocks, now=now)
    ok("block containing a signed transaction accepted")

    balances = state.utxos.balances()
    assert balances[bob_addr] == amount + k.block_subsidy(12) + fee, balances[bob_addr]
    ok(f"bob holds {k.format_amount(balances[bob_addr])} CRUX (10 received + reward + fee)")

    emitted = chainmod.emitted_supply(state.height)
    assert emitted == state.circulating(), (emitted, state.circulating())
    ok(f"supply conserved: {k.format_amount(emitted)} CRUX emitted == unspent")

    # ---- coinbase maturity ---------------------------------------------
    young_txid = blocks[12].txs[0].txid()
    bad_spend = k.Tx(
        inputs=[k.TxIn(young_txid, 0, bob_pub, "")],
        outputs=[k.TxOut(1 * k.COIN, alice_addr)],
    )
    sign_tx(bad_spend, bob_priv)
    expect_reject(
        "immature coinbase cannot be spent",
        lambda: mine_block(blocks, "octocat", bob_addr, txs=[bad_spend]),
    )

    # ---- retarget ------------------------------------------------------
    while len(blocks) < 17:
        blocks.append(mine_block(blocks, "crux", alice_addr))
    state = chainmod.replay(blocks, now=now)
    b16 = blocks[16]
    assert b16.height == 16
    assert b16.bits == chainmod.bits_for_height(16, blocks[:16])
    ok(f"difficulty retargeted at height 16 (bits {b16.bits:#010x})")

    good = list(blocks)

    # ---- tampering -----------------------------------------------------
    t = copy.deepcopy(good)
    subset = rp.decode_solution(t[5].solution)
    t[5].solution = rp.encode_solution(subset ^ 1)
    expect_reject("altered subset breaks proof of work", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[5].solution = t[5].solution + "00"
    expect_reject("padded solution (proof is not constant-size) rejected",
                  lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[12].txs[1].outputs[0].value += 1
    expect_reject("altered output breaks the merkle root", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[3].txs[0].outputs[0].value += 1
    t[3].merkle_root = k.merkle_root([x.txid() for x in t[3].txs])
    expect_reject("inflated coinbase rejected", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[12].txs[1].inputs[0].sig = "00" * 64
    t[12].merkle_root = k.merkle_root([x.txid() for x in t[12].txs])
    expect_reject("forged signature rejected", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[12].miner = "someone-else"
    expect_reject("stealing a block by renaming the miner invalidates the puzzle",
                  lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[7].nonce += 1
    expect_reject("changing the nonce without re-solving rejected",
                  lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    del t[8]
    for i, b in enumerate(t):
        b.height = i
    expect_reject("removing a block breaks the hash chain", lambda: chainmod.replay(t, now=now))

    t = copy.deepcopy(good)
    t[9].bits = k.POW_LIMIT_BITS ^ 0x00010000
    expect_reject("mining at the wrong difficulty rejected", lambda: chainmod.replay(t, now=now))

    dbl = k.Tx(
        inputs=[k.TxIn(gen_coinbase_txid, 0, alice_pub, "")],
        outputs=[k.TxOut(1 * k.COIN, alice_addr)],
    )
    sign_tx(dbl, alice_priv)
    expect_reject(
        "double spend of an already-spent output rejected",
        lambda: mine_block(good, "crux", alice_addr, txs=[dbl]),
    )

    stale = mine_block(good[:13], "crux", alice_addr)
    expect_reject(
        "block built on a stale tip rejected",
        lambda: chainmod.replay(good + [stale], now=now),
    )

    # ---- hash target is actually enforced --------------------------------
    # Build a header, solve the knapsack, then demand an impossible target.
    easy = copy.deepcopy(good[1])
    assert rp.check_subset(easy.header_core(), rp.decode_solution(easy.solution))
    digest = easy.digest()
    assert rp.hash_meets_target(digest, k.bits_to_target(easy.bits))
    assert not rp.hash_meets_target(digest, 1)
    ok("hash target rejects a digest that sits above it")

    # ---- proof size is constant ------------------------------------------
    sizes = {len(bytes.fromhex(b.solution)) for b in good}
    assert sizes == {rp.SOLUTION_BYTES}, sizes
    ok(f"every block's proof is exactly {rp.SOLUTION_BYTES} bytes")

    # ---- final state ---------------------------------------------------
    final = chainmod.replay(good, now=now)
    print()
    print(f"  {len(PASSED)} checks passed")
    print(f"  height {final.height}  tip {final.tip_hash[:24]}…")
    print(f"  chainwork {final.chainwork:,} expected hashes")
    print(f"  supply {k.format_amount(final.circulating())} CRUX")
    return 0


if __name__ == "__main__":
    orig_n, orig_b = 40, 38
    try:
        raise SystemExit(run())
    finally:
        rp.N, rp.B = orig_n, orig_b
