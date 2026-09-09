# CRUX consensus specification

Version 1.0. This document defines every rule a CRUX block must satisfy.
It is normative: `crux/consensus.py` is the reference implementation, and
where the two disagree, this document is wrong and should be fixed.

CRUX is a Bitcoin-derived chain with two unusual properties: its canonical
ledger is a file in a GitHub repository; and its proof of work is a
subset-sum puzzle under a hash target, so mining rewards a better solver
rather than better silicon, *and* difficulty can rise forever without the
proof getting bigger.

Consensus is enforced by a workflow that any observer can re-run, and by
`verify.py`, which trusts nothing but `chain/blocks.jsonl`.

CRUX coins have no value, no market, and no bridge to anything. The chain
exists to be read.

---

## 1. Units

| | |
|---|---|
| Coin | CRUX |
| Base unit | grain |
| 1 CRUX | 100 000 000 grains |

All amounts in blocks are integers denominated in grains. Fractional grains
do not exist.

## 2. Chain parameters

| Parameter | CRUX | Bitcoin |
|---|---|---|
| Initial subsidy | 50 CRUX | 50 BTC |
| Halving interval | 210 000 blocks | 210 000 blocks |
| Money supply | 21 000 000 CRUX | 21 000 000 BTC |
| Retarget interval | 16 blocks | 2016 blocks |
| Target block spacing | 600 s | 600 s |
| Retarget clamp | ×4 / ÷4 | ×4 / ÷4 |
| Coinbase maturity | 10 blocks | 100 blocks |
| Median time span | 11 blocks | 11 blocks |
| Max future drift | 7200 s | 7200 s |
| Proof-of-work limit | `0x2100ffff` (work ≈ 1) | `0x1d00ffff` |
| Genesis difficulty | `0x20040000` (work = 64) | `0x1d00ffff` |
| Work function | 1 × subset-sum, n = 44, under a hash target | double SHA-256 |
| Proof size | 8 bytes, constant | 4-byte nonce |
| Max transactions per block | 32 | ~4000 |
| Max submission size | 24 576 bytes | — |
| Coinbase message | ≤ 80 bytes (≤ 256 at genesis) | ≤ 100 bytes |
| Transfer memo | ≤ 120 bytes | — |

One n=44 puzzle is meet-in-the-middle at 2²² — about sixteen times the
work of n=40 — so each attempt is seconds on the reference solver, not
milliseconds. Genesis asks for expected work 64, which is minutes of
grinding. Ten-minute spacing is then the hash target's job, the same
600 s Bitcoin uses. Retargeting hunts it at 4× per 16-block window,
with no ceiling: a faster solver makes blocks *harder*, not just *faster*.

The floor is deliberately not at zero: difficulty falls during quiet
stretches, and without a floor a chain nobody is mining would hand out
free hashes. A block at the floor still costs one solvable knapsack.

## 3. Hashing

`H(x)` denotes SHA-256. All chain hashes use `H(H(x))`, written `H²`, as
Bitcoin does. Digests are interpreted as **big-endian** integers when
compared to a target. (Bitcoin's little-endian quirk is not reproduced.)

## 4. Serialisation

CRUX serialises to a canonical UTF-8 byte string rather than a binary
format. Fields are joined with `|`, which is forbidden inside any
user-supplied field, so the encoding is unambiguous.

### 4.1 Transaction core

```
v{version}|cb:{cb_height}:{hex(message)}|out:{value}:{address}|…|lt{locktime}
v{version}|in:{txid}:{vout}:{pubkey}|…|memo:{hex(memo)}|out:{value}:{address}|…|lt{locktime}
```

The first form is the coinbase, the second a spend. The memo is inside the
core, so it is committed to by both the txid and every signature: a note
cannot be altered, stripped or added in transit.

`txid = H²(core)`.

Signatures are **not** part of the core. Public keys **are**. A transaction
therefore has exactly one txid no matter how it was signed, which removes
Bitcoin's pre-segwit malleability by construction rather than by patch.

### 4.2 Signature hash

```
sighash = H²(core) = txid
```

Every input signs the same digest, committing to all inputs, all outputs,
and every public key involved. This is equivalent to Bitcoin's `SIGHASH_ALL`
with no other sighash modes available.

### 4.3 Block header

The header comes in two pieces. The **core** is what the puzzle is seeded
from, and excludes the solution, since the solution is the answer to the
puzzle the core defines:

```
core   = {height}|{prev_hash}|{merkle_root}|{timestamp}|{bits:08x}|{miner}|{algo}|{nonce}
header = core|{solution}
```

`block_hash = H²(header)`, so block identity commits to the answer as well
as the question.

The miner's GitHub handle is **inside the core**. Two consequences, and both
matter: every miner is working on a different puzzle, so a solution posted
publicly is useless to anyone else; and a solved block cannot be
re-submitted under a different handle without redoing all of the work. This
is what makes an untrusted relay channel safe.

`nonce` is a 32-bit integer, also inside the core. Grinding it produces a
fresh knapsack instance.

`algo` selects the work function. Only `1` (subset-sum under a hash target)
is defined. The field exists so a second work function can be added without
a new header format.

`solution` is an 8-byte big-endian subset mask, hex-encoded. It is the same
length at height 0 and at height 1 000 000.

## 5. Proof of work

Bitcoin asks for a nonce whose header hashes below a target. CRUX asks for
the same thing, except each nonce attempt is a knapsack.

### 5.1 The puzzle

```
seed = H²(core)
a_i  = H(seed ‖ i) mod 2^b            for i = 0 … n−1
S    = Σ a_i
T    = S/2 + (H(seed ‖ "target") mod 2·⌊S/16⌋) − ⌊S/16⌋
```

with `n = 44` and `b = n − 2 = 42`. Any `a_i` that comes out zero is set to 1.

A solution is a non-empty subset of `{0 … n−1}` whose elements sum to exactly
`T`, submitted as the 8-byte mask.

Two constants there are load-bearing.

**Why `b = n − 2`.** Density — how wide the numbers are relative to how many
there are — decides whether subset-sum is hard. Below roughly 0.94, lattice
reduction solves instances outright. Here density is `n/b ≈ 1.05`, just
inside the hard regime.

**Why `T` sits near `S/2`.** Subset sums are not spread evenly; they pile up
around half the total the way sums of coin flips pile up around the middle.
A target drawn uniformly across the range lands in the tail where almost no
subsets reach. Placing `T` near the mean puts it where the subsets are:
about 55% of instances are then solvable.

### 5.2 The grind

An instance is random, so it may have no solution at all. A miner who finds
none increments `nonce` and gets a fresh instance. When a subset is found,
the miner computes `H²(header)` and keeps grinding if it sits above the
compact target. That two-stage grind is what makes mining a lottery rather
than a footrace, exactly as nonce grinding is in Bitcoin.

### 5.3 Difficulty

The best known practical attack on one instance is meet-in-the-middle:
enumerate every subset sum of each half and look for a pair adding to `T`.
That costs about `2^(n/2)` time **and memory**. Difficulty does **not**
come from growing `n` or from repeating the puzzle.

It comes from the hash target, as it does in Bitcoin:

```
work   = 2²⁵⁶ ÷ (target + 1)
valid  ⇔  subset sums to T  ∧  H²(header) ≤ target
```

`bits` uses Bitcoin's compact nBits encoding: an exponent byte followed by a
three-byte mantissa, `target = mantissa · 256^(exponent−3)`, sign bit clear.

Verification is 44 additions and two SHA-256s at every height. A block at
the highest difficulty is the same size as a block at the floor.

### 5.4 Why not k puzzles

Repeating the puzzle `k` times makes the proof `k` times larger. A ceiling
on `k` then becomes a ceiling on real work: once miners are faster than the
ceiling allows, blocks stay easy and the payload keeps growing, until it
hits GitHub's 64 KB issue-body limit and Actions environment-variable
limits. CRUX does not do that.

## 6. Difficulty adjustment

At every height that is a multiple of 16 and greater than zero:

```
actual   = timestamp[h−1] − timestamp[h−16]
actual   = clamp(actual, TIMESPAN/4, TIMESPAN·4)
target'  = target · actual / TIMESPAN
target'  = min(target', POW_LIMIT)
```

where `TIMESPAN = 16 · 600` seconds (2 h 40 m). At all other heights, `bits` must
equal the previous block's `bits`.

**Deviation.** Bitcoin reads the first block of the *previous* window rather
than the first block of the window being closed — an off-by-one present since
2009 that makes each retarget cover 2015 intervals instead of 2016. CRUX uses
the correct window. This is the one place CRUX knowingly diverges from
Bitcoin's behaviour rather than its parameters.

A 4× clamp per window means a sudden 1000× hashrate takes five windows
(80 blocks, a bit over half a day at target spacing) to catch. After that,
ten-minute blocks resume. There is no cap on how high `work` can go.

## 7. Subsidy

```
subsidy(height) = 50 CRUX >> (height // 210_000)
```

Zero after 64 halvings. Lifetime issuance is just under 21 million CRUX —
the same cap as Bitcoin. The coinbase output total must equal
`subsidy(height) + sum(fees)` **exactly**.

**Deviation.** Bitcoin permits a miner to claim *less* than the full reward,
which has permanently destroyed a small amount of BTC. CRUX requires the
exact amount, so total supply is a pure function of height and `verify.py`
can assert that emitted supply equals unspent supply.

## 8. Transaction validity

A non-coinbase transaction is valid if and only if:

1. `version` is 1.
2. It has 1–8 outputs and at most 8 inputs.
3. Every output value is a positive integer within range.
4. Every output address passes bech32 validation with HRP `crux`.
5. No outpoint appears twice among its inputs.
6. Every input references an outpoint that is currently unspent.
7. Any coinbase output it spends is at least 10 blocks old.
8. For each input, `address(pubkey) == address` of the output being spent.
9. For each input, the signature verifies against that public key over the
   sighash, with `s ≤ n/2` (low-s; high-s signatures are rejected outright).
10. The sum of outputs does not exceed the sum of inputs.
11. The memo is ≤ 120 bytes and contains no control characters and no `|`.

The difference between inputs and outputs is the fee, claimable by the miner.

## 9. Block validity

1. Miner handle is 1–39 characters of `[A-Za-z0-9-]`.
2. Height is exactly one greater than the tip; `prev_hash` equals the tip's
   hash. Genesis is height 0 with a null `prev_hash`.
3. `bits` equals the value required by §6.
4. Timestamp is at most 7200 s in the future, and strictly greater than the
   median of the previous 11 block timestamps.
5. The block has 1–32 transactions; the first is the coinbase and no other
   transaction is.
6. No two transactions share a txid.
7. The merkle root commits to the transaction list (§10).
8. `algo` is 1, `nonce` is in `[0, 2³²)`, `solution` decodes to exactly 8
   bytes, the subset solves the header's knapsack, and `H²(header) ≤ target`
   (§5).
9. The coinbase message contains no control characters and no `|`, and is
   ≤ 80 bytes — except at height 0, where the cap is 256 bytes.
10. **BIP 34.** The coinbase commits to its own block height in `cb_height`,
    which must equal the block height. Without this, two blocks with the same
    miner, reward and message would produce the same coinbase txid.
11. **BIP 30.** No transaction in the block may share a txid with an existing
    unspent transaction.
12. Every non-coinbase transaction satisfies §8, evaluated in order against a
    UTXO set that already reflects earlier transactions in the same block.
13. The coinbase pays exactly `subsidy + fees`.

Rules 10 and 11 exist for the same reason they exist in Bitcoin: duplicate
coinbase txids silently overwrite earlier unspent outputs and destroy coins.

## 10. Merkle root

Bitcoin's construction, including the rule that an odd node at any level is
duplicated to pair with itself. An empty transaction list is not permitted.

The duplication rule alone would allow two different transaction lists to
produce the same root (CVE-2012-2459). Rule 9.6 — no duplicate txids in a
block — closes this.

## 11. Addresses

```
address = bech32(hrp="crux", version=0, payload=H(pubkey)[:20])
```

Public keys are 33-byte SEC1 compressed secp256k1 points — the same curve
and encoding Bitcoin uses.

**Deviation.** Bitcoin's payload is `RIPEMD160(SHA256(pubkey))`. RIPEMD160
is absent from many modern OpenSSL builds, and CRUX commits to depending on
nothing outside the Python standard library, so the payload is the first 20
bytes of a single SHA-256 instead. The encoding and checksum are BIP 173
unchanged.

## 12. Signatures

ECDSA over secp256k1 with RFC 6979 deterministic nonces. Signatures are
64-byte compact `r ‖ s`, both big-endian, with `s` normalised to the lower
half of the curve order per BIP 62. High-s signatures are invalid, not
merely non-standard.

Deterministic nonces mean signing the same transaction twice produces
identical bytes, so a transaction has one canonical encoding end to end.

## 13. Chain selection

The chain is the sequence of blocks in `chain/blocks.jsonl`. A submitted
block must extend the current tip; blocks building on any earlier block are
rejected as stale. Cumulative chainwork is tracked and reported but is not
used to reorganise.

**Deviation.** Bitcoin follows the most-work chain and reorganises when a
heavier one appears. CRUX has a single serialised writer — one workflow, one
concurrency group — so competing chains cannot form. Two miners who solve
the same height race on submission time, and the loser is told the new tip
and can mine again. This is a real limitation and is the honest cost of
using a git repository as the network.

## 14. Relay

Blocks and transactions are relayed as compact payloads prefixed
`crux-block-v1:` and `crux-tx-v1:`. A payload is canonical JSON, sorted
keys, no whitespace, then base64. The decoded JSON is rejected above
24 576 bytes.

Two ways to submit, and they do the same thing:

1. **A new GitHub issue**, labelled `crux`, whose body is the payload line.
   The node processes it, replies, and **closes the issue**. The next
   submission is a new issue. There is no standing comment thread.
2. **A pull request** that adds exactly one file under `inbox/`. The node
   reads the file through the API as inert text, applies it to `main`, and
   closes the PR. It is never merged.

The submitting account's login is taken from the event payload, never from
the submission itself.

A pull request is **never merged**. This has two consequences: concurrent
submissions cannot conflict, and no code from a fork is ever executed by a
workflow holding a write token — the standard `pull_request_target`
failure mode.

A block is mined by whoever submits it, and the miner's handle is fixed
inside the header (§4.3), so the relay channel never has to be trusted.

**Why not a standing issue with comments.** GitHub issue bodies and comments
are 64 KB. A proof that grows with difficulty, plus a reply on every
submission, plus years of comments on one thread, hits that limit and also
blows up Actions logs and `github.event.*.body` environment variables.
CRUX's proof is 8 bytes at every height, each submission is its own
document, and the node-side cap is 24 KB. The limits never come into
scope.

## 15. Identity (not consensus)

A GitHub handle may be bound to an address by posting, from that account:

```
crux-id-v1:{handle}:{pubkey}:{sig}
```

where `sig` is over `H²("crux-identity-v1|" ‖ handle)`. The signature proves
control of the key; posting from the account proves control of the handle.
Neither alone is sufficient.

Bindings live in `chain/registry.json` and decide only whose name appears
beside a balance in the rendered ledger. They are **not** part of consensus,
carry no authority over funds, and `verify.py` ignores the file entirely.
Transactions are authorised by signatures and nothing else.

## 16. Threat notes

- **Impersonation** is prevented by the miner handle living inside the header
  core (§4.3), which the puzzle is seeded from.
- **Solution theft** — copying a solved block out of a public issue —
  is prevented the same way: a different handle means a different puzzle.
- **Theft** is prevented by §8.8–8.9; there is no scripting language and no
  path to spending an output without its private key.
- **Inflation** is prevented by §9.13 and checked globally by `verify.py`,
  which asserts emitted supply equals unspent supply on every run.
- **Spam** is rate-limited by proof of work. Invalid submissions are rejected
  in milliseconds and cost the chain nothing.
- **Payload overflow** is prevented by a constant-size proof (§5.3) and a
  24 KB submission cap (§14).
- **Untrusted fork code** is never executed: pull requests are read through
  the API and applied by the base repository, never checked out (§14).
- **A malicious repository owner** can rewrite `chain/blocks.jsonl` at will.
  They cannot forge proof of work or signatures, so any rewrite is detectable
  by anyone holding an earlier copy — but it is not preventable. A chain
  whose ledger is one person's repository is trusting that person's restraint,
  and pretending otherwise would be dishonest.

## 17. Genesis

Genesis is height 0, `prev_hash` all zeroes, mined at `0x20040000`, with a
coinbase message of up to 256 bytes. The message is fixed at creation and is
part of the chain's identity; changing it invalidates every block after it.
