#!/usr/bin/env python3
"""
Production difficulty: retarget must climb without a ceiling, clamp at 4×
per window, never fall through the floor, and leave the proof 8 bytes.

Uses the live constants (n=44, genesis work 64, 10-minute spacing). Does
not mine — it drives next_bits with timestamps, which is the whole rule.

    python3 tests/test_difficulty.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crux import consensus as k
from crux import pow as rp


PASSED = []


def ok(label):
    PASSED.append(label)
    print(f"  ok    {label}")


def work(bits: int) -> int:
    return k.bits_to_work(bits)


def retarget(prev_bits: int, span: int) -> int:
    """Bits for height 16 given a window that lasted `span` seconds."""
    return k.next_bits(k.RETARGET_INTERVAL, prev_bits, 0, span)


def run():
    print("CRUX production difficulty")
    print()

    assert rp.N == 44 and rp.B == 42, (rp.N, rp.B)
    assert abs(rp.N / rp.B - 1.05) < 0.02
    ok(f"knapsack n={rp.N} b={rp.B} density {rp.N / rp.B:.3f} (hard regime)")

    assert rp.SOLUTION_BYTES == 8
    assert (rp.N + 7) // 8 <= rp.SOLUTION_BYTES
    ok("proof stays 8 bytes at n=44")

    assert k.TARGET_SPACING == 600
    assert k.RETARGET_INTERVAL == 16
    assert k.MAX_ADJUST_FACTOR == 4
    ok("10-minute spacing, 16-block window, 4× clamp — same shape as Bitcoin")

    assert k.HALVING_INTERVAL == 210_000
    assert k.MAX_MONEY == 21_000_000 * k.COIN
    assert k.max_supply() <= k.MAX_MONEY
    assert k.max_supply() > 20_000_000 * k.COIN
    ok(f"money supply cap {k.max_supply() / k.COIN:,.0f} CRUX (21 million, same as Bitcoin)")

    gen = work(k.GENESIS_BITS)
    floor = work(k.POW_LIMIT_BITS)
    # nBits work is floor(2^256/(target+1)); genesis target is 2^250 so work is 63.
    assert 60 <= gen <= 64, gen
    assert floor == 1, floor
    assert round(k.difficulty(k.GENESIS_BITS)) == 64
    ok(f"genesis work {gen} (difficulty {k.difficulty(k.GENESIS_BITS):.0f}), floor work {floor}")

    # Off-boundary heights must copy the previous bits.
    assert k.next_bits(1, k.GENESIS_BITS, 0, 1) == k.GENESIS_BITS
    assert k.next_bits(15, k.GENESIS_BITS, 0, 1) == k.GENESIS_BITS
    assert k.next_bits(0, k.GENESIS_BITS, 0, 1) == k.GENESIS_BITS
    ok("difficulty is sticky between retargets")

    # Instant window (span 0) clamps to TIMESPAN/4 → target ÷ 4, work × 4.
    fast = retarget(k.GENESIS_BITS, 0)
    assert k.bits_to_target(fast) == k.bits_to_target(k.GENESIS_BITS) // 4
    assert work(fast) > gen
    ratio = work(fast) / gen
    assert 3.9 < ratio < 4.2, ratio
    ok(f"fast window: work {gen} → {work(fast)} ({ratio:.2f}×)")

    # Same if timestamps go backwards (time-warp): still the 4× harder clamp.
    warp = retarget(k.GENESIS_BITS, -10_000)
    assert warp == fast
    ok("negative window (time warp) still clamps to 4× harder, never easier")

    # On time: target unchanged.
    ontime = retarget(k.GENESIS_BITS, k.TARGET_TIMESPAN)
    assert ontime == k.GENESIS_BITS
    ok("on-time window leaves genesis bits unchanged")

    # Slow window: 4× easier, but not through the floor.
    slow = retarget(k.GENESIS_BITS, 10**9)
    assert k.bits_to_target(slow) == k.bits_to_target(k.GENESIS_BITS) * 4
    assert work(slow) < gen
    ok(f"slow window: work {gen} → {work(slow)} (target ×4)")

    floor_bits = retarget(k.POW_LIMIT_BITS, 10**9)
    assert work(floor_bits) >= floor
    assert k.bits_to_target(floor_bits) <= k.bits_to_target(k.POW_LIMIT_BITS)
    ok("a slow window at the floor cannot go easier than POW_LIMIT")

    # Unbounded climb: six instant windows, 4^6 = 4096×.
    bits = k.GENESIS_BITS
    series = [work(bits)]
    print()
    print("  simulated instant windows (what a GPU flood looks like)")
    print("    window  work          difficulty     vs 10-min target")
    for i in range(8):
        bits = retarget(bits, 0)
        series.append(work(bits))
        # reference CPU ~0.75 n=44 puzzles/s, ~55% solvable → ~0.4 solutions/s
        # expected seconds ≈ work / 0.4. Just report work; spacing follows.
        print(f"    {i + 1:>6}  {series[-1]:<12,}  {k.difficulty(bits):<14,.0f}  "
              f"{'harder' if series[-1] > series[-2] else 'stuck'}")
    assert series[-1] > series[0] * 1000
    for a, b in zip(series, series[1:]):
        assert b > a
        assert 3.5 < b / a < 4.5
    climbed_bits = bits
    ok(f"8 fast windows: work {series[0]} → {series[-1]:,} with no ceiling")

    # A mixed history: fast, then on-time, then slow, then fast again.
    bits = k.GENESIS_BITS
    bits = retarget(bits, 0)                       # ×4
    bits = retarget(bits, k.TARGET_TIMESPAN)       # same
    held = work(bits)
    bits = retarget(bits, k.TARGET_TIMESPAN * 4)   # ÷4 back
    assert abs(work(bits) - gen) <= 1
    bits = retarget(bits, 0)
    assert work(bits) > gen
    assert held > gen
    ok("fast → on-time → slow → fast: work follows the windows, not a cap")

    # Production instance is 44 numbers in the hard-density band.
    numbers, target = rp.instance(b"crux-difficulty-probe|0")
    assert len(numbers) == 44
    assert all(n > 0 for n in numbers)
    assert min(numbers) >= 1
    total = sum(numbers)
    assert total // 4 < target < total * 3 // 4  # T sits near the mean
    ok("live instance is 44 positive numbers with T near S/2")

    print()
    print(f"  {len(PASSED)} checks passed")
    print(f"  genesis difficulty {k.difficulty(k.GENESIS_BITS):.0f}  "
          f"bits {k.GENESIS_BITS:#010x}  work {gen}")
    print(f"  after 8 saturated windows work would be {series[-1]:,} "
          f"(difficulty {k.difficulty(climbed_bits):,.0f}) and the proof is still 8 bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
