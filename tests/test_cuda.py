#!/usr/bin/env python3
"""
CUDA knapsack solver tests.

Always checks that the CPU path still works and that the CUDA module
reports availability without crashing. When a GPU is present, compares
GPU masks against pow.check_subset.

    python3 tests/test_cuda.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crux import pow as rp

PASSED = []


def ok(label):
    PASSED.append(label)
    print(f"  ok    {label}")


def run():
    print("CRUX CUDA solver")
    print()

    orig_n, orig_b = rp.N, rp.B
    rp.N, rp.B = 16, 14
    try:
        core = b"cuda-test|0|deadbeef|1|2100ffff|miner|1|0"
        numbers, target = None, None
        cpu = None
        used = core
        for nonce in range(64):
            used = core + str(nonce).encode()
            numbers, target = rp.instance(used)
            cpu = rp.solve_instance_cpu(numbers, target)
            if cpu is not None:
                break
        assert cpu is not None, "no solvable n=16 instance in 64 nonces"
        assert rp.check_subset(used, cpu)
        ok("CPU solver still finds or correctly rejects an n=16 instance")

        none = rp.solve_instance_cpu([1, 2, 4, 8] + [1] * 12, 10**12)
        assert none is None
        ok("CPU solver returns None when no subset exists")

        from crux import pow_cuda

        avail = pow_cuda.available()
        assert avail in (True, False)
        ok(f"pow_cuda.available() is {avail} (backend {pow_cuda.backend()})")

        rp.set_cuda(False)
        assert rp.cuda_wanted() is False
        forced_cpu = rp.solve_instance(numbers, target, use_cuda=False)
        assert forced_cpu == cpu
        ok("use_cuda=False stays on the CPU path")
        rp.set_cuda(None)

        if not avail:
            print("  skip  no GPU in this environment")
            ok("CUDA tests skipped without a device")
            return 0

        gpu = pow_cuda.solve_instance(numbers, target)
        assert gpu is not None
        assert rp.check_subset(used, gpu)
        ok(f"GPU mask {gpu:#x} solves the same instance (CPU {cpu:#x})")

        via_pow = rp.solve_instance(numbers, target, use_cuda=True)
        assert via_pow is not None
        assert rp.check_subset(used, via_pow)
        ok("pow.solve_instance(use_cuda=True) returns a valid mask")

        info = rp.cuda_info()
        assert info.startswith("cuda:")
        ok(f"cuda_info reports {info}")
    finally:
        rp.N, rp.B = orig_n, orig_b
        rp.set_cuda(None)

    print()
    print(f"  {len(PASSED)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
