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

.visible .entry k_fill_idx(
    .param .u64 p_idx,
    .param .u32 p_size
)
{
    .reg .pred %p;
    .reg .u32 %i, %tidx, %ntidx, %ctaidx, %size;
    .reg .u64 %idx, %addr, %off, %v;

    ld.param.u64 %idx, [p_idx];
    ld.param.u32 %size, [p_size];
    mov.u32 %tidx, %tid.x;
    mov.u32 %ntidx, %ntid.x;
    mov.u32 %ctaidx, %ctaid.x;
    mad.lo.u32 %i, %ntidx, %ctaidx, %tidx;
    setp.ge.u32 %p, %i, %size;
    @%p ret;
    cvt.u64.u32 %v, %i;
    mul.wide.u32 %off, %i, 8;
    add.u64 %addr, %idx, %off;
    st.global.u64 [%addr], %v;
    ret;
}

.visible .entry k_bitonic_step(
    .param .u64 p_keys,
    .param .u64 p_vals,
    .param .u32 p_n,
    .param .u32 p_j,
    .param .u32 p_k
)
{
    .reg .pred %p, %asc, %gt, %lt, %eq, %vgt, %vlt, %eqgt, %eqlt, %swapa, %swapd, %swap;
    .reg .u32 %i, %ixj, %tidx, %ntidx, %ctaidx, %n, %j, %k, %ik;
    .reg .u64 %keys, %vals, %offi, %offj, %ai, %aj, %bi, %bj, %ki, %kj, %vi, %vj;

    ld.param.u64 %keys, [p_keys];
    ld.param.u64 %vals, [p_vals];
    ld.param.u32 %n, [p_n];
    ld.param.u32 %j, [p_j];
    ld.param.u32 %k, [p_k];
    mov.u32 %tidx, %tid.x;
    mov.u32 %ntidx, %ntid.x;
    mov.u32 %ctaidx, %ctaid.x;
    mad.lo.u32 %i, %ntidx, %ctaidx, %tidx;
    xor.b32 %ixj, %i, %j;
    setp.le.u32 %p, %ixj, %i;
    @%p ret;
    setp.ge.u32 %p, %i, %n;
    @%p ret;
    setp.ge.u32 %p, %ixj, %n;
    @%p ret;

    mul.wide.u32 %offi, %i, 8;
    mul.wide.u32 %offj, %ixj, 8;
    add.u64 %ai, %keys, %offi;
    add.u64 %aj, %keys, %offj;
    add.u64 %bi, %vals, %offi;
    add.u64 %bj, %vals, %offj;
    ld.global.u64 %ki, [%ai];
    ld.global.u64 %kj, [%aj];
    ld.global.u64 %vi, [%bi];
    ld.global.u64 %vj, [%bj];

    and.b32 %ik, %i, %k;
    setp.eq.u32 %asc, %ik, 0;
    setp.gt.u64 %gt, %ki, %kj;
    setp.lt.u64 %lt, %ki, %kj;
    setp.eq.u64 %eq, %ki, %kj;
    setp.gt.u64 %vgt, %vi, %vj;
    setp.lt.u64 %vlt, %vi, %vj;
    and.pred %eqgt, %eq, %vgt;
    or.pred %swapa, %gt, %eqgt;
    and.pred %eqlt, %eq, %vlt;
    or.pred %swapd, %lt, %eqlt;
    not.pred %p, %asc;
    and.pred %swapa, %swapa, %asc;
    and.pred %swapd, %swapd, %p;
    or.pred %swap, %swapa, %swapd;
    @!%swap ret;

    st.global.u64 [%ai], %kj;
    st.global.u64 [%aj], %ki;
    st.global.u64 [%bi], %vj;
    st.global.u64 [%bj], %vi;
    ret;
}

.visible .entry k_search(
    .param .u64 p_keys,
    .param .u64 p_vals,
    .param .u64 p_right,
    .param .u32 p_nleft,
    .param .u32 p_nright,
    .param .u64 p_target,
    .param .u32 p_half,
    .param .u64 p_found,
    .param .u64 p_mask
)
{
    .reg .pred %p, %hit;
    .reg .u32 %r, %tidx, %ntidx, %ctaidx, %nleft, %nright, %lo, %hi, %mid, %d, %half, %old, %one;
    .reg .u64 %keys, %vals, %right, %found, %mout, %s, %need, %target, %off, %addr, %km, %vm, %full, %r64, %sh, %zero;

    ld.param.u64 %keys, [p_keys];
    ld.param.u64 %vals, [p_vals];
    ld.param.u64 %right, [p_right];
    ld.param.u32 %nleft, [p_nleft];
    ld.param.u32 %nright, [p_nright];
    ld.param.u64 %target, [p_target];
    ld.param.u32 %half, [p_half];
    ld.param.u64 %found, [p_found];
    ld.param.u64 %mout, [p_mask];

    mov.u32 %tidx, %tid.x;
    mov.u32 %ntidx, %ntid.x;
    mov.u32 %ctaidx, %ctaid.x;
    mad.lo.u32 %r, %ntidx, %ctaidx, %tidx;
    setp.ge.u32 %p, %r, %nright;
    @%p ret;

    mul.wide.u32 %off, %r, 8;
    add.u64 %addr, %right, %off;
    ld.global.u64 %s, [%addr];
    setp.gt.u64 %p, %s, %target;
    @%p ret;
    sub.u64 %need, %target, %s;

    mov.u32 %lo, 0;
    mov.u32 %hi, %nleft;
bs:
    setp.ge.u32 %p, %lo, %hi;
    @%p bra bs_done;
    sub.u32 %d, %hi, %lo;
    shr.u32 %d, %d, 1;
    add.u32 %mid, %lo, %d;
    mul.wide.u32 %off, %mid, 8;
    add.u64 %addr, %keys, %off;
    ld.global.u64 %km, [%addr];
    setp.lt.u64 %p, %km, %need;
    @%p bra go_right;
    mov.u32 %hi, %mid;
    bra bs;
go_right:
    add.u32 %lo, %mid, 1;
    bra bs;
bs_done:
    setp.ge.u32 %p, %lo, %nleft;
    @%p ret;
    mul.wide.u32 %off, %lo, 8;
    add.u64 %addr, %keys, %off;
    ld.global.u64 %km, [%addr];
    setp.ne.u64 %p, %km, %need;
    @%p ret;
    add.u64 %addr, %vals, %off;
    ld.global.u64 %vm, [%addr];
    cvt.u64.u32 %r64, %r;
    shl.b64 %sh, %r64, %half;
    or.b64 %full, %vm, %sh;
    mov.u64 %zero, 0;
    setp.eq.u64 %p, %full, %zero;
    @%p ret;
    mov.u32 %one, 1;
    atom.global.cas.b32 %old, [%found], 0, %one;
    setp.ne.u32 %p, %old, 0;
    @%p ret;
    st.global.u64 [%mout], %full;
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
        lib.cuMemsetD8.argtypes = [c_ulonglong, ctypes.c_ubyte, ctypes.c_size_t]
        lib.cuMemsetD8.restype = c_int

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
        self.fn_sums = self._load_fn(b"k_subset_sums")
        self.fn_fill = self._load_fn(b"k_fill_idx")
        self.fn_bitonic = self._load_fn(b"k_bitonic_step")
        self.fn_search = self._load_fn(b"k_search")
        self._cap = 0
        self._d_nums = c_ulonglong(0)
        self._d_left = c_ulonglong(0)
        self._d_right = c_ulonglong(0)
        self._d_idx = c_ulonglong(0)
        self._d_found = c_ulonglong(0)
        self._d_mask = c_ulonglong(0)
        self._host_nums = None

    def _load_fn(self, name: bytes):
        fn = c_void_p()
        _check(self.lib.cuModuleGetFunction(byref(fn), self.mod, name), name.decode())
        return fn

    def _launch(self, fn, count, params):
        threads = 256
        blocks = max(1, (int(count) + threads - 1) // threads)
        arr = (c_void_p * len(params))(*[ctypes.addressof(p) for p in params])
        _check(
            self.lib.cuLaunchKernel(fn, blocks, 1, 1, threads, 1, 1, 0, None, arr, None),
            "cuLaunchKernel",
        )

    def _ensure(self, size: int) -> None:
        import numpy as np

        if size <= self._cap:
            return
        for attr in ("_d_left", "_d_right", "_d_idx", "_d_nums", "_d_found", "_d_mask"):
            ptr = getattr(self, attr)
            if ptr.value:
                self.lib.cuMemFree(ptr)
            setattr(self, attr, c_ulonglong(0))
        _check(self.lib.cuMemAlloc(byref(self._d_nums), 64 * 8), "cuMemAlloc nums")
        _check(self.lib.cuMemAlloc(byref(self._d_left), size * 8), "cuMemAlloc left")
        _check(self.lib.cuMemAlloc(byref(self._d_right), size * 8), "cuMemAlloc right")
        _check(self.lib.cuMemAlloc(byref(self._d_idx), size * 8), "cuMemAlloc idx")
        _check(self.lib.cuMemAlloc(byref(self._d_found), 4), "cuMemAlloc found")
        _check(self.lib.cuMemAlloc(byref(self._d_mask), 8), "cuMemAlloc mask")
        self._host_nums = np.empty(64, dtype=np.uint64)
        self._cap = size

    def _launch_sums(self, numbers, d_out):
        n = len(numbers)
        size = 1 << n
        self._host_nums[:n] = numbers
        _check(
            self.lib.cuMemcpyHtoD(self._d_nums, int(self._host_nums.ctypes.data), n * 8),
            "HtoD nums",
        )
        self._launch(
            self.fn_sums,
            size,
            [
                c_ulonglong(self._d_nums.value),
                c_uint(n),
                c_ulonglong(d_out.value),
                c_uint(size),
            ],
        )

    def solve(self, numbers, target: int):
        n = len(numbers)
        half = n // 2
        nright = n - half
        ls = 1 << half
        rs = 1 << nright
        self._ensure(max(ls, rs))
        self._launch_sums(numbers[:half], self._d_left)
        self._launch_sums(numbers[half:], self._d_right)
        self._launch(self.fn_fill, ls, [c_ulonglong(self._d_idx.value), c_uint(ls)])
        k = 2
        while k <= ls:
            j = k // 2
            while j > 0:
                self._launch(
                    self.fn_bitonic,
                    ls,
                    [
                        c_ulonglong(self._d_left.value),
                        c_ulonglong(self._d_idx.value),
                        c_uint(ls),
                        c_uint(j),
                        c_uint(k),
                    ],
                )
                j //= 2
            k *= 2
        _check(self.lib.cuMemsetD8(self._d_found, 0, 4), "memset found")
        _check(self.lib.cuMemsetD8(self._d_mask, 0, 8), "memset mask")
        self._launch(
            self.fn_search,
            rs,
            [
                c_ulonglong(self._d_left.value),
                c_ulonglong(self._d_idx.value),
                c_ulonglong(self._d_right.value),
                c_uint(ls),
                c_uint(rs),
                c_ulonglong(int(target)),
                c_uint(half),
                c_ulonglong(self._d_found.value),
                c_ulonglong(self._d_mask.value),
            ],
        )
        _check(self.lib.cuCtxSynchronize(), "cuCtxSynchronize")
        found = c_uint(0)
        mask = c_ulonglong(0)
        _check(self.lib.cuMemcpyDtoH(ctypes.addressof(found), self._d_found, 4), "DtoH found")
        if not found.value:
            return None
        _check(self.lib.cuMemcpyDtoH(ctypes.addressof(mask), self._d_mask, 8), "DtoH mask")
        return int(mask.value) or None


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


_FIND = None


def _find_fn():
    global _FIND
    if _FIND is not None:
        return _FIND
    try:
        import numpy as np
        from numba import njit

        @njit(cache=True)
        def find(sorted_sums, order, right, target, half):
            n = sorted_sums.size
            for r in range(right.size):
                s = right[r]
                if s > target:
                    continue
                need = target - s
                lo = 0
                hi = n
                while lo < hi:
                    mid = (lo + hi) >> 1
                    if sorted_sums[mid] < need:
                        lo = mid + 1
                    else:
                        hi = mid
                if lo < n and sorted_sums[lo] == need:
                    full = int(order[lo]) | (r << half)
                    if full:
                        return full
            return 0

        z = np.zeros(1, dtype=np.uint64)
        find(z, z, z, np.uint64(0), 1)
        _FIND = find
        return _FIND
    except Exception:
        _FIND = False
        return False


def _match(left_sums, right_sums, half, target):
    try:
        import numpy as np
    except ImportError:
        import bisect

        pairs = sorted((int(s), m) for m, s in enumerate(left_sums))
        keys = [p[0] for p in pairs]
        for r, s in enumerate(right_sums):
            s = int(s)
            if s > target:
                continue
            i = bisect.bisect_left(keys, target - s)
            if i < len(pairs) and pairs[i][0] == target - s:
                full = pairs[i][1] | (r << half)
                if full:
                    return full
        return None

    left = np.asarray(left_sums, dtype=np.uint64)
    right = np.asarray(right_sums, dtype=np.uint64)
    order = np.argsort(left, kind="quicksort").astype(np.uint64, copy=False)
    sorted_sums = left[order]
    fn = _find_fn()
    tgt = np.uint64(target)
    if fn:
        found = int(fn(sorted_sums, order, right, tgt, np.int64(half)))
        return found or None
    need = tgt - right
    idx = np.searchsorted(sorted_sums, need, side="left")
    n = left.size
    in_range = idx < n
    clipped = np.minimum(idx, n - 1)
    hit = in_range & (right <= tgt) & (sorted_sums[clipped] == need)
    loc = int(np.argmax(hit))
    if hit[loc]:
        full = int(order[int(idx[loc])]) | (int(loc) << half)
        return full or None
    return None


def _solve_driver(numbers, target: int):
    return _STATE["drv"].solve(numbers, int(target))


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
