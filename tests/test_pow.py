#!/usr/bin/env python3
"""Unit tests for the CRUX work function: one knapsack under a hash target."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crux import consensus as k
from crux import crypto
from crux import pow as rp
from crux.wire import encode_json, find_payload, decode_json, BLOCK_PREFIX


def run():
    print("CRUX pow / wire tests")
    n = 0

    def ok(label):
        nonlocal n
        n += 1
        print(f"  ok    {label}")

    # density sits in the hard regime
    assert abs(rp.N / rp.B - 1.05) < 0.02
    ok(f"density n/b = {rp.N / rp.B:.3f} (hard regime)")

    # proof is constant size
    assert rp.SOLUTION_BYTES == 8
    subset = (1 << 13) | 1
    blob = rp.encode_solution(subset)
    assert len(bytes.fromhex(blob)) == 8
    assert rp.decode_solution(blob) == subset
    try:
        rp.decode_solution(blob + "00")
        raise AssertionError("padded solution accepted")
    except ValueError:
        ok("padded solution rejected at decode")

    # a real instance at n=16 so this is instant
    old_n, old_b = rp.N, rp.B
    rp.N, rp.B = 16, 14
    try:
        core = b"test-header|0|deadbeef|1|20200000|miner|1|0"
        numbers, target = rp.instance(core)
        assert len(numbers) == 16
        found = None
        for nonce in range(64):
            c = core[:-1] + str(nonce).encode()
            numbers, target = rp.instance(c)
            found = rp.solve_instance(numbers, target)
            if found:
                assert rp.check_subset(c, found)
                assert not rp.check_subset(c, found ^ 1)
                break
        assert found, "no solvable n=16 instance in 64 nonces"
        ok("meet-in-the-middle finds a real subset and rejects a flipped bit")
    finally:
        rp.N, rp.B = old_n, old_b

    # hash target
    digest = crypto.sha256d(b"anything")
    huge = (1 << 256) - 1
    assert rp.hash_meets_target(digest, huge)
    assert not rp.hash_meets_target(digest, 1)
    ok("hash target accepts below, rejects above")

    # compact bits round-trip for genesis / limit
    for bits in (k.POW_LIMIT_BITS, k.GENESIS_BITS, 0x1D00FFFF):
        t = k.bits_to_target(bits)
        back = k.target_to_bits(t)
        # nBits is lossy in the trailing bits of the mantissa; the decoded
        # target of the re-encoded form must match.
        assert k.bits_to_target(back) == t
    ok("nBits encode/decode preserves the target")

    genesis_work = k.target_to_work(k.bits_to_target(k.GENESIS_BITS))
    floor_work = k.target_to_work(k.bits_to_target(k.POW_LIMIT_BITS))
    assert genesis_work > floor_work
    ok(f"genesis work {genesis_work} > floor work {floor_work}")

    # wire: small payload, size cap
    payload = encode_json({"height": 1, "solution": "00" * 8})
    assert len(payload) < 200
    kind, rest = find_payload("hello\n" + BLOCK_PREFIX + payload + "\n")
    assert kind == "block"
    assert decode_json(rest)["height"] == 1
    ok("wire codec round-trips a compact block payload")

    print()
    print(f"  {n} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
