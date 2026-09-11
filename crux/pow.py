"""
CRUX proof of work: one subset-sum puzzle under a hash target.

Bitcoin asks for a nonce whose header hashes below a target. CRUX asks for
the same thing, except each nonce attempt is a knapsack instance rather than
a round of SHA-256.

    1. Derive one n=44 subset-sum instance from the header (nonce inside it).
    2. Find a non-empty subset that sums to the instance target, or grind
       the nonce and try a fresh instance.
    3. Accept the block only if H²(header ‖ solution) <= the compact target.

The proof is a single 8-byte subset mask. It does not grow when difficulty
rises. Difficulty is the hash target, the same nBits encoding Bitcoin uses,
so it can climb without bound and a block stays a few hundred bytes at any
height. That is what keeps submissions inside GitHub's 64 KB issue limit.

n=44 is meet-in-the-middle at 2²² — sixteen times the work of n=40 — so
each attempt is a real puzzle, not a hash. The 10-minute block spacing
then comes from how many of those attempts the target demands, exactly
as Bitcoin spaces blocks by how many SHA-256s the target demands.

Verification is 44 additions and two SHA-256s.

## Why not k puzzles

Repeating the puzzle k times makes the proof k times larger. A ceiling on k
then becomes a ceiling on real work: once miners are faster than the ceiling
allows, blocks stay easy and the payload keeps growing. CRUX does not do
that. One puzzle, one mask, a hash target that retargets forever.

## The puzzle

    seed  = H²(header_core)            # header_core includes miner and nonce
    a_i   = H(seed ‖ i) mod 2^b        # n numbers, i = 0 … n−1
    S     = Σ a_i
    T     = S/2 ± (H(seed ‖ "target") mod S/16)

with n = 44 and b = n − 2, density n/b ≈ 1.05 — just inside the regime
where lattice reduction does not solve instances outright.

T sits near S/2 because subset sums pile up around the mean. A uniform
target lands in the tail where almost no subsets reach; near the mean,
about 55% of instances have a solution. The rest are why mining is a
lottery: grind the nonce.

The miner's GitHub handle is inside header_core, so everyone works on
different puzzles and a solution posted publicly is worth nothing to
anyone else.
"""

import hashlib

from . import crypto

N = 44  # numbers per puzzle; fixed. Hash target is what scales with hashrate.
B = N - 2  # bit width of each number, giving density n/b ≈ 1.05
SOLUTION_BYTES = 8  # subset mask; 8 bytes covers n up to 64 without a format change
MAX_NONCE = 1 << 32


def instance(header_core: bytes):
    """Derive the single puzzle for this header. Deterministic and cheap."""
    seed = crypto.sha256d(header_core)
    mask = (1 << B) - 1
    numbers = [
        (int.from_bytes(hashlib.sha256(seed + i.to_bytes(4, "big")).digest(), "big") & mask) or 1
        for i in range(N)
    ]
    total = sum(numbers)
    spread = max(1, total // 16)
    offset = int.from_bytes(hashlib.sha256(seed + b"target").digest(), "big") % (2 * spread)
    return numbers, total // 2 + offset - spread


def check_subset(header_core: bytes, subset: int) -> bool:
    """Verify the knapsack. O(n) additions."""
    if subset <= 0 or subset >= (1 << N):
        return False
    numbers, target = instance(header_core)
    total = 0
    for i in range(N):
        if subset >> i & 1:
            total += numbers[i]
    return total == target


def encode_solution(subset: int) -> str:
    """Pack a subset mask as hex. Always SOLUTION_BYTES, at any difficulty."""
    if subset <= 0 or subset >= (1 << (8 * SOLUTION_BYTES)):
        raise ValueError("subset out of range")
    return subset.to_bytes(SOLUTION_BYTES, "big").hex()


def decode_solution(blob: str) -> int:
    raw = bytes.fromhex(blob)
    if len(raw) != SOLUTION_BYTES:
        raise ValueError(
            f"solution must be exactly {SOLUTION_BYTES} bytes, got {len(raw)}"
        )
    return int.from_bytes(raw, "big")


def hash_meets_target(digest: bytes, target: int) -> bool:
    """True if the 32-byte digest, as a big-endian integer, is <= target."""
    if len(digest) != 32 or target <= 0:
        return False
    return int.from_bytes(digest, "big") <= target


def verify(header_core: bytes, solution: str, digest: bytes, target: int) -> bool:
    """
    Full proof of work: the subset solves the header's puzzle, and the
    block hash sits at or below the compact target.
    """
    try:
        subset = decode_solution(solution)
    except ValueError:
        return False
    if not check_subset(header_core, subset):
        return False
    return hash_meets_target(digest, target)


# --------------------------------------------------------------------------
# solving
# --------------------------------------------------------------------------


def _half_sums(numbers):
    """
    Every subset sum of `numbers`, indexed so sums[m] is the sum of the
    elements whose bit is set in m. Built by doubling, which is both the
    fastest way to do this in Python and the reason the index is the mask.
    """
    sums = [0]
    for a in numbers:
        sums += [s + a for s in sums]
    return sums


def solve_instance_cpu(numbers, target: int):
    """Meet in the middle on CPU. Returns a subset mask, or None if there is none."""
    n = len(numbers)
    half = n // 2
    table = {}
    for mask, s in enumerate(_half_sums(numbers[:half])):
        if s <= target and s not in table:
            table[s] = mask
    for rmask, s in enumerate(_half_sums(numbers[half:])):
        if s > target:
            continue
        lmask = table.get(target - s)
        if lmask is None:
            continue
        full = lmask | (rmask << half)
        if full:
            return full
    return None


# None = auto (use CUDA when a device is present). Tests and --cpu set False.
_USE_CUDA = None


def set_cuda(enabled):
    """True/False to force a backend; None to auto-detect."""
    global _USE_CUDA
    _USE_CUDA = enabled


def cuda_wanted() -> bool:
    if _USE_CUDA is False:
        return False
    if _USE_CUDA is True:
        return True
    try:
        from . import pow_cuda
        return pow_cuda.available()
    except Exception:
        return False


def cuda_info() -> str:
    try:
        from . import pow_cuda
        if pow_cuda.available():
            name = pow_cuda.device_name() or pow_cuda.backend()
            return f"cuda:{name}"
    except Exception:
        pass
    return "cpu"


def solve_instance(numbers, target: int, use_cuda=None):
    """
    Meet in the middle. Uses CUDA when a GPU is available unless `use_cuda`
    is False. Consensus does not care which backend found the mask.

    Auto mode only sends production-sized puzzles (n >= 32) to the GPU so
    the tiny n=16 test instances stay on the instant CPU path.
    """
    want = cuda_wanted() if use_cuda is None else bool(use_cuda)
    n = len(numbers)
    if want and (use_cuda is True or n >= 32):
        try:
            from . import pow_cuda
            if pow_cuda.available():
                return pow_cuda.solve_instance(numbers, target)
        except Exception:
            pass
    return solve_instance_cpu(numbers, target)


def solve_header(header_core_fn, start_nonce: int = 0, max_nonce: int = MAX_NONCE):
    """
    Grind nonces until the derived instance has a solution.

    `header_core_fn(nonce) -> bytes` rebuilds the header core for each try.
    Returns (nonce, subset). Does not check the hash target — the miner
    does that and keeps grinding if the hash is still too high.
    """
    for nonce in range(start_nonce, max_nonce):
        core = header_core_fn(nonce)
        numbers, target = instance(core)
        subset = solve_instance(numbers, target)
        if subset is not None:
            return nonce, subset
    raise RuntimeError("no solvable instance in the nonce range; this should not happen")
