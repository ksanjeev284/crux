"""
Optional CUDA backend for the CRUX knapsack solver.

Consensus never imports this. Verification stays in pow.py (44 additions).
Mining may call solve_instance() here when an NVIDIA driver is present.

The primary backend is the CUDA *driver* API plus a small PTX kernel, so
there is no nvcc, Numba, or CuPy requirement — only a working GPU driver.
Compiled knapsack.cu / Numba are tried afterwards if the driver path fails.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import POINTER, byref, c_char_p, c_int, c_uint, c_ulonglong, c_void_p, create_string_buffer

_STATE = {
    "probed": False,
    "ok": False,
    "backend": "cpu",
    "device": "",
    "lib": None,
    "drv": None,
}

PTX = r"""
.version 6.4
.target sm_50
.address_size 64

.visible .entry k_subset_sums(
    .param .u64 p_nums,
    .param .u32 p_n,
    .param .u64 p_out,
    .param .u32 p_size
)
{
    .reg .pred %p;
    .reg .u32 %mask, %n, %size, %i, %bit, %tidx, %ntidx, %ctaidx, %one;
    .reg .u64 %nums, %out, %s, %addr, %val, %off;

    ld.param.u64 %nums, [p_nums];
    ld.param.u32 %n, [p_n];
    ld.param.u64 %out, [p_out];
    ld.param.u32 %size, [p_size];

    mov.u32 %tidx, %tid.x;
    mov.u32 %ntidx, %ntid.x;
    mov.u32 %ctaidx, %ctaid.x;
    mad.lo.u32 %mask, %ntidx, %ctaidx, %tidx;

    setp.ge.u32 %p, %mask, %size;
    @%p ret;

    mov.u64 %s, 0;
    mov.u32 %i, 0;
loop:
    setp.ge.u32 %p, %i, %n;
    @%p bra done;
    mov.u32 %one, 1;
    shl.b32 %bit, %one, %i;
    and.b32 %bit, %mask, %bit;
    setp.eq.u32 %p, %bit, 0;
    @%p bra next;
    mul.wide.u32 %off, %i, 8;
    add.u64 %addr, %nums, %off;
    ld.global.u64 %val, [%addr];
    add.u64 %s, %s, %val;
next:
    add.u32 %i, %i, 1;
    bra loop;
done:
    mul.wide.u32 %off, %mask, 8;
    add.u64 %addr, %out, %off;
    st.global.u64 [%addr], %s;
    ret;
}
"""


def _env_disabled() -> bool:
    val = (os.environ.get("CRUX_CUDA") or "").strip().lower()
    return val in {"0", "false", "no", "off", "cpu"}


def _lib_name() -> str:
    return "crux_pow_cuda.dll" if sys.platform == "win32" else "crux_pow_cuda.so"


def _cache_dir() -> str:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(root, "CRUX")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/CRUX")
    root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(root, "crux")


def _cu_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cuda", "knapsack.cu")


def _check(err, what):
    if err != 0:
        raise RuntimeError(f"{what} failed: CUDA_ERROR {err}")


class _Driver:
    def __init__(self, lib):
        self.lib = lib
        lib.cuInit.argtypes = [c_uint]
        lib.cuInit.restype = c_int
        lib.cuDeviceGetCount.argtypes = [POINTER(c_int)]
        lib.cuDeviceGetCount.restype = c_int
        lib.cuDeviceGet.argtypes = [POINTER(c_int), c_int]
        lib.cuDeviceGet.restype = c_int
        lib.cuDeviceGetName.argtypes = [c_char_p, c_int, c_int]
        lib.cuDeviceGetName.restype = c_int
        lib.cuCtxCreate.argtypes = [POINTER(c_void_p), c_uint, c_int]
        lib.cuCtxCreate.restype = c_int
        lib.cuMemAlloc.argtypes = [POINTER(c_ulonglong), ctypes.c_size_t]
        lib.cuMemAlloc.restype = c_int
        lib.cuMemFree.argtypes = [c_ulonglong]
        lib.cuMemFree.restype = c_int
        lib.cuMemcpyHtoD.argtypes = [c_ulonglong, c_void_p, ctypes.c_size_t]
        lib.cuMemcpyHtoD.restype = c_int
        lib.cuMemcpyDtoH.argtypes = [c_void_p, c_ulonglong, ctypes.c_size_t]
        lib.cuMemcpyDtoH.restype = c_int
        lib.cuModuleLoadData.argtypes = [POINTER(c_void_p), c_void_p]
        lib.cuModuleLoadData.restype = c_int
        lib.cuModuleGetFunction.argtypes = [POINTER(c_void_p), c_void_p, c_char_p]
        lib.cuModuleGetFunction.restype = c_int
        lib.cuLaunchKernel.argtypes = [
            c_void_p, c_uint, c_uint, c_uint, c_uint, c_uint, c_uint,
            c_uint, c_void_p, POINTER(c_void_p), POINTER(c_void_p),
        ]
        lib.cuLaunchKernel.restype = c_int
        lib.cuCtxSynchronize.argtypes = []
        lib.cuCtxSynchronize.restype = c_int

        _check(lib.cuInit(0), "cuInit")
        count = c_int(0)
        _check(lib.cuDeviceGetCount(byref(count)), "cuDeviceGetCount")
        if count.value < 1:
            raise RuntimeError("no CUDA device")
        dev = c_int(0)
        _check(lib.cuDeviceGet(byref(dev), 0), "cuDeviceGet")
        buf = create_string_buffer(128)
        lib.cuDeviceGetName(buf, 128, dev)
        self.name = buf.value.decode("utf-8", "replace")
        self.ctx = c_void_p()
        _check(lib.cuCtxCreate(byref(self.ctx), 0, dev), "cuCtxCreate")
        self.mod = c_void_p()
        ptx = PTX.encode("utf-8") + b"\x00"
        _check(lib.cuModuleLoadData(byref(self.mod), ptx), "cuModuleLoadData")
        self.fn = c_void_p()
        _check(
            lib.cuModuleGetFunction(byref(self.fn), self.mod, b"k_subset_sums"),
            "cuModuleGetFunction",
        )

    def subset_sums(self, numbers):
        import array

        n = len(numbers)
        size = 1 << n
        host = array.array("Q", [int(x) & ((1 << 64) - 1) for x in numbers])
        out = array.array("Q", [0]) * size
        d_nums = c_ulonglong(0)
        d_out = c_ulonglong(0)
        try:
            _check(self.lib.cuMemAlloc(byref(d_nums), n * 8), "cuMemAlloc nums")
            _check(self.lib.cuMemAlloc(byref(d_out), size * 8), "cuMemAlloc out")
            _check(self.lib.cuMemcpyHtoD(d_nums, host.buffer_info()[0], n * 8), "HtoD")
            threads = 256
            blocks = (size + threads - 1) // threads
            p_nums = c_ulonglong(d_nums.value)
            p_n = c_uint(n)
            p_out = c_ulonglong(d_out.value)
            p_size = c_uint(size)
            args = (c_void_p * 4)(
                ctypes.addressof(p_nums),
                ctypes.addressof(p_n),
                ctypes.addressof(p_out),
                ctypes.addressof(p_size),
            )
            _check(
                self.lib.cuLaunchKernel(
                    self.fn, blocks, 1, 1, threads, 1, 1, 0, None, args, None
                ),
                "cuLaunchKernel",
            )
            _check(self.lib.cuCtxSynchronize(), "cuCtxSynchronize")
            _check(self.lib.cuMemcpyDtoH(out.buffer_info()[0], d_out, size * 8), "DtoH")
        finally:
            if d_nums.value:
                self.lib.cuMemFree(d_nums)
            if d_out.value:
                self.lib.cuMemFree(d_out)
        return out


def _load_driver():
    names = ["nvcuda.dll"] if sys.platform == "win32" else ["libcuda.so.1", "libcuda.so"]
    last = None
    for name in names:
        try:
            lib = ctypes.CDLL(name)
            return _Driver(lib)
        except Exception as exc:
            last = exc
    raise RuntimeError(f"CUDA driver not loaded: {last}")


def _match(left_sums, right_sums, half, target):
    try:
        import numpy as np

        left = np.frombuffer(left_sums, dtype=np.uint64)
        right = np.frombuffer(right_sums, dtype=np.uint64)
        order = np.argsort(left, kind="mergesort")
        sorted_sums = left[order]
        tgt = np.uint64(target)
        need = tgt - right
        idx = np.searchsorted(sorted_sums, need, side="left")
        in_range = idx < left.size
        clipped = np.clip(idx, 0, left.size - 1)
        match = in_range & (right <= tgt) & (sorted_sums[clipped] == need)
        for r in np.flatnonzero(match):
            full = int(order[int(idx[r])]) | (int(r) << half)
            if full:
                return full
        return None
    except ImportError:
        import bisect

        left = list(left_sums)
        pairs = sorted((s, m) for m, s in enumerate(left))
        keys = [p[0] for p in pairs]
        for r, s in enumerate(right_sums):
            if s > target:
                continue
            i = bisect.bisect_left(keys, target - s)
            if i < len(pairs) and pairs[i][0] == target - s:
                full = pairs[i][1] | (r << half)
                if full:
                    return full
        return None


def _solve_driver(numbers, target: int):
    drv = _STATE["drv"]
    n = len(numbers)
    half = n // 2
    left = drv.subset_sums(numbers[:half])
    right = drv.subset_sums(numbers[half:])
    return _match(left, right, half, int(target))


def _try_load_nvcc_lib(path: str):
    if not path or not os.path.isfile(path):
        return None
    try:
        lib = ctypes.CDLL(path)
        lib.crux_cuda_available.restype = c_int
        lib.crux_cuda_solve.argtypes = [
            POINTER(ctypes.c_uint64),
            c_int,
            ctypes.c_uint64,
            POINTER(ctypes.c_uint64),
        ]
        lib.crux_cuda_solve.restype = c_int
        if lib.crux_cuda_available() != 1:
            return None
        return lib
    except OSError:
        return None


def _try_compile():
    src = _cu_path()
    if not os.path.isfile(src):
        return ""
    out = os.path.join(_cache_dir(), _lib_name())
    if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cmd = ["nvcc", "-O3", "--shared", "-allow-unsupported-compiler", "-o", out, src]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0 or not os.path.isfile(out):
        return ""
    return out


def _probe() -> None:
    if _STATE["probed"]:
        return
    _STATE["probed"] = True
    if _env_disabled():
        return

    try:
        drv = _load_driver()
        _STATE["ok"] = True
        _STATE["backend"] = "driver"
        _STATE["drv"] = drv
        _STATE["device"] = drv.name
        return
    except Exception:
        pass

    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cuda", _lib_name())
    for candidate in (here, os.path.join(_cache_dir(), _lib_name()), _try_compile()):
        lib = _try_load_nvcc_lib(candidate)
        if lib is not None:
            _STATE["ok"] = True
            _STATE["backend"] = "nvcc"
            _STATE["lib"] = lib
            _STATE["device"] = "CUDA (nvcc)"
            return

    try:
        from numba import cuda

        if cuda.is_available() and cuda.gpus:
            cuda.select_device(0)
            name = cuda.get_current_device().name
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            _STATE["ok"] = True
            _STATE["backend"] = "numba"
            _STATE["device"] = str(name)
    except Exception:
        return


def available() -> bool:
    _probe()
    return bool(_STATE["ok"])


def backend() -> str:
    _probe()
    return _STATE["backend"] if _STATE["ok"] else "cpu"


def device_name() -> str:
    _probe()
    return _STATE["device"] if _STATE["ok"] else ""


def _solve_nvcc(numbers, target: int):
    lib = _STATE["lib"]
    n = len(numbers)
    arr = (ctypes.c_uint64 * n)(*[int(x) for x in numbers])
    out = ctypes.c_uint64(0)
    rc = lib.crux_cuda_solve(arr, n, int(target), byref(out))
    if rc == 1:
        return int(out.value)
    if rc == 0:
        return None
    raise RuntimeError("crux_cuda_solve failed")


_K_SUMS = None


def _numba_kernel():
    global _K_SUMS
    if _K_SUMS is not None:
        return _K_SUMS
    from numba import cuda, uint64

    @cuda.jit
    def k_sums(nums, nitems, out):
        mask = cuda.grid(1)
        limit = 1 << nitems
        if mask >= limit:
            return
        s = uint64(0)
        m = mask
        for i in range(nitems):
            if m & (1 << i):
                s += nums[i]
        out[mask] = s

    _K_SUMS = k_sums
    return _K_SUMS


def _solve_numba(numbers, target: int):
    import numpy as np
    from numba import cuda

    n = len(numbers)
    half = n // 2
    nright = n - half
    ls = 1 << half
    rs = 1 << nright
    k_sums = _numba_kernel()

    def run_half(vals, count, size):
        d_nums = cuda.to_device(np.asarray(vals, dtype=np.uint64))
        d_out = cuda.device_array(size, dtype=np.uint64)
        threads = 256
        blocks = (size + threads - 1) // threads
        k_sums[blocks, threads](d_nums, count, d_out)
        return d_out.copy_to_host()

    left = run_half(numbers[:half], half, ls)
    right = run_half(numbers[half:], nright, rs)
    return _match(left, right, half, int(target))


def solve_instance(numbers, target: int):
    """Return a subset mask or None. Raises if the GPU backend cannot run."""
    _probe()
    if not _STATE["ok"]:
        raise RuntimeError("CUDA is not available")
    nums = [int(x) for x in numbers]
    tgt = int(target)
    if _STATE["backend"] == "driver":
        return _solve_driver(nums, tgt)
    if _STATE["backend"] == "nvcc":
        return _solve_nvcc(nums, tgt)
    return _solve_numba(nums, tgt)
