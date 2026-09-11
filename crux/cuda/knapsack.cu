/*
 * CRUX knapsack solver — optional CUDA backend.
 *
 * Consensus does not use this file. Verification is still 44 additions in
 * Python. This only searches for a subset mask; any valid mask is accepted.
 *
 * Build (from the repo root):
 *   nvcc -O3 --shared -o crux_pow_cuda.dll crux/cuda/knapsack.cu     # Windows
 *   nvcc -O3 --shared -o crux_pow_cuda.so  crux/cuda/knapsack.cu     # Linux
 */

#include <cuda_runtime.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#endif

__global__ void k_subset_sums(const uint64_t* nums, int n, uint64_t* out, int size) {
    int mask = blockIdx.x * blockDim.x + threadIdx.x;
    if (mask >= size) {
        return;
    }
    uint64_t s = 0;
    unsigned m = (unsigned)mask;
    for (int i = 0; i < n; i++) {
        if (m & (1u << i)) {
            s += nums[i];
        }
    }
    out[mask] = s;
}

struct Pair {
    uint64_t sum;
    uint32_t mask;
};

static int cmp_pair(const void* a, const void* b) {
    const Pair* pa = (const Pair*)a;
    const Pair* pb = (const Pair*)b;
    if (pa->sum < pb->sum) {
        return -1;
    }
    if (pa->sum > pb->sum) {
        return 1;
    }
    if (pa->mask < pb->mask) {
        return -1;
    }
    if (pa->mask > pb->mask) {
        return 1;
    }
    return 0;
}

static int find_sum(const Pair* left, int n, uint64_t need) {
    int lo = 0;
    int hi = n;
    while (lo < hi) {
        int mid = lo + (hi - lo) / 2;
        if (left[mid].sum < need) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    if (lo < n && left[lo].sum == need) {
        return lo;
    }
    return -1;
}

static int launch_sums(const uint64_t* nums, int n, uint64_t* host_out, int size) {
    uint64_t* d_nums = NULL;
    uint64_t* d_out = NULL;
    int rc = -1;
    if (cudaMalloc((void**)&d_nums, (size_t)n * sizeof(uint64_t)) != cudaSuccess) {
        goto done;
    }
    if (cudaMalloc((void**)&d_out, (size_t)size * sizeof(uint64_t)) != cudaSuccess) {
        goto done;
    }
    if (cudaMemcpy(d_nums, nums, (size_t)n * sizeof(uint64_t), cudaMemcpyHostToDevice) != cudaSuccess) {
        goto done;
    }
    {
        int threads = 256;
        int blocks = (size + threads - 1) / threads;
        k_subset_sums<<<blocks, threads>>>(d_nums, n, d_out, size);
        if (cudaDeviceSynchronize() != cudaSuccess) {
            goto done;
        }
    }
    if (cudaMemcpy(host_out, d_out, (size_t)size * sizeof(uint64_t), cudaMemcpyDeviceToHost) != cudaSuccess) {
        goto done;
    }
    rc = 0;
done:
    if (d_nums) {
        cudaFree(d_nums);
    }
    if (d_out) {
        cudaFree(d_out);
    }
    return rc;
}

extern "C" EXPORT int crux_cuda_available(void) {
    int count = 0;
    if (cudaGetDeviceCount(&count) != cudaSuccess) {
        return 0;
    }
    return count > 0 ? 1 : 0;
}

extern "C" EXPORT int crux_cuda_solve(
    const uint64_t* numbers,
    int n,
    uint64_t target,
    uint64_t* out_mask
) {
    if (!numbers || !out_mask || n < 2 || n > 62) {
        return -1;
    }
    int half = n / 2;
    int nright = n - half;
    int ls = 1 << half;
    int rs = 1 << nright;

    uint64_t* left_sums = (uint64_t*)malloc((size_t)ls * sizeof(uint64_t));
    uint64_t* right_sums = (uint64_t*)malloc((size_t)rs * sizeof(uint64_t));
    Pair* left = (Pair*)malloc((size_t)ls * sizeof(Pair));
    if (!left_sums || !right_sums || !left) {
        free(left_sums);
        free(right_sums);
        free(left);
        return -1;
    }

    if (launch_sums(numbers, half, left_sums, ls) != 0) {
        free(left_sums);
        free(right_sums);
        free(left);
        return -1;
    }
    if (launch_sums(numbers + half, nright, right_sums, rs) != 0) {
        free(left_sums);
        free(right_sums);
        free(left);
        return -1;
    }

    for (int i = 0; i < ls; i++) {
        left[i].sum = left_sums[i];
        left[i].mask = (uint32_t)i;
    }
    qsort(left, (size_t)ls, sizeof(Pair), cmp_pair);

    int found = 0;
    for (int r = 0; r < rs; r++) {
        uint64_t s = right_sums[r];
        if (s > target) {
            continue;
        }
        int idx = find_sum(left, ls, target - s);
        if (idx < 0) {
            continue;
        }
        uint64_t full = (uint64_t)left[idx].mask | ((uint64_t)r << half);
        if (full == 0) {
            continue;
        }
        *out_mask = full;
        found = 1;
        break;
    }

    free(left_sums);
    free(right_sums);
    free(left);
    return found;
}
