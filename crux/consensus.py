"""
CRUX consensus rules.

Everything a node needs to decide whether a block is valid: serialisation,
compact difficulty targets, retargeting, merkle roots, the subsidy schedule,
the UTXO set, and full transaction and block validation.

Deviations from Bitcoin are deliberate and are listed in SPEC.md.
"""

from dataclasses import dataclass, field

from . import crypto
from . import pow as powfn

# --------------------------------------------------------------------------
# chain parameters
# --------------------------------------------------------------------------

CHAIN_NAME = "CRUX"
TICKER = "CRUX"
BASE_UNIT = "grain"
COIN = 100_000_000  # grains per CRUX

INITIAL_SUBSIDY = 50 * COIN
HALVING_INTERVAL = 210_000  # blocks, same as Bitcoin
MAX_HALVINGS = 64
# Geometric sum of the subsidy schedule: 210_000 × 50 × (2 − 2^{-64}) CRUX.
# Capped at 21 million for output checks, matching Bitcoin's money supply.
MAX_MONEY = 21_000_000 * COIN

RETARGET_INTERVAL = 16  # blocks; Bitcoin uses 2016
TARGET_SPACING = 600  # seconds between blocks we aim for, same as Bitcoin
TARGET_TIMESPAN = RETARGET_INTERVAL * TARGET_SPACING
MAX_ADJUST_FACTOR = 4  # clamp, same as Bitcoin

# Easiest target the chain will ever accept: essentially every hash passes,
# so a block is still one solvable knapsack (n=44, seconds on the reference
# miner). Quiet stretches cannot go below this.
POW_LIMIT_BITS = 0x2100FFFF
# Genesis: expected work 64. Combined with n=44 that is minutes of grinding
# on the reference solver, not seconds. Retargeting then hunts the 10-minute
# spacing the same way Bitcoin does — 4× per window, no ceiling.
GENESIS_BITS = 0x20040000

COINBASE_MATURITY = 10  # blocks; Bitcoin uses 100
MEDIAN_TIME_SPAN = 11  # blocks, same as Bitcoin
MAX_FUTURE_DRIFT = 2 * 3600  # seconds, same as Bitcoin

MAX_TXS_PER_BLOCK = 32  # keeps a block well under the 24 KB submission cap
MAX_MESSAGE_BYTES = 80  # coinbase message, same size as Bitcoin's field
MAX_GENESIS_MESSAGE_BYTES = 256  # genesis is special-cased, as it is in Bitcoin
MAX_MINER_LEN = 39  # GitHub's maximum username length
MAX_MEMO_BYTES = 120  # note attached to a transfer
MAX_OUTPUTS = 8
MAX_INPUTS = 8

ALGO_SUBSET_SUM = 1  # the only work function; the field is reserved for more

# Wire-format cap. GitHub issue bodies and comments are 64 KB. A CRUX block
# with 32 transactions is a few kilobytes; 24 KB leaves headroom and is the
# node-side rejection threshold so Actions never see a huge payload.
MAX_SUBMISSION_BYTES = 24_576


class ConsensusError(Exception):
    """Raised when a block or transaction breaks a consensus rule."""


def _as_int(value, name: str) -> int:
    """JSON bool is a subclass of int; reject it, and reject floats."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _as_str(value, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _as_list(value, name: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _as_dict(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _is_hex(s: str, n: int) -> bool:
    if len(s) != n:
        return False
    try:
        bytes.fromhex(s)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------
# compact difficulty targets (Bitcoin's nBits encoding)
# --------------------------------------------------------------------------


def bits_to_target(bits: int) -> int:
    """Decode compact nBits into a 256-bit target."""
    exponent = bits >> 24
    mantissa = bits & 0x007FFFFF
    if bits & 0x00800000:
        raise ConsensusError("negative target")
    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    target = mantissa << (8 * (exponent - 3))
    if target >= 1 << 256:
        raise ConsensusError("target overflows 256 bits")
    return target


def target_to_bits(target: int) -> int:
    """Encode a target back into compact nBits form."""
    if target <= 0:
        raise ConsensusError("target must be positive")
    raw = target.to_bytes(32, "big").lstrip(b"\x00")
    if not raw:
        return 0
    if raw[0] & 0x80:  # keep the sign bit clear
        raw = b"\x00" + raw
    exponent = len(raw)
    mantissa = int.from_bytes(raw[:3].ljust(3, b"\x00"), "big")
    return (exponent << 24) | mantissa


def target_to_work(target: int) -> int:
    """Expected hashes needed to beat this target. Bitcoin's chainwork formula."""
    return (1 << 256) // (target + 1)


def bits_to_work(bits: int) -> int:
    """Expected hashes at this compact target."""
    return target_to_work(bits_to_target(bits))


def difficulty(bits: int) -> float:
    """Difficulty relative to the chain's easiest allowed target."""
    return bits_to_target(POW_LIMIT_BITS) / bits_to_target(bits)


def next_bits(height: int, prev_bits: int, first_ts: int, last_ts: int) -> int:
    """
    Difficulty for the block at `height`.

    Retargets every RETARGET_INTERVAL blocks from the time actually taken by
    the previous window, clamped to a factor of 4 in either direction.
    Unlike Bitcoin this reads the true first block of the window, without the
    off-by-one that Bitcoin has carried since 2009.
    """
    if height % RETARGET_INTERVAL != 0 or height == 0:
        return prev_bits

    actual = last_ts - first_ts
    low = TARGET_TIMESPAN // MAX_ADJUST_FACTOR
    high = TARGET_TIMESPAN * MAX_ADJUST_FACTOR
    actual = max(low, min(high, actual))

    new_target = bits_to_target(prev_bits) * actual // TARGET_TIMESPAN
    limit = bits_to_target(POW_LIMIT_BITS)
    new_target = min(new_target, limit)
    if new_target <= 0:
        new_target = 1
    return target_to_bits(new_target)


# --------------------------------------------------------------------------
# subsidy
# --------------------------------------------------------------------------


def block_subsidy(height: int) -> int:
    halvings = height // HALVING_INTERVAL
    if halvings >= MAX_HALVINGS:
        return 0
    return INITIAL_SUBSIDY >> halvings


def total_supply_at(height: int) -> int:
    """Sum of all subsidies up to and including `height`."""
    total = 0
    for h in range(height + 1):
        total += block_subsidy(h)
    return total


def max_supply() -> int:
    """Lifetime issuance if every subsidy is claimed. Just under 21 million CRUX."""
    total = 0
    subsidy = INITIAL_SUBSIDY
    for _ in range(MAX_HALVINGS):
        total += HALVING_INTERVAL * subsidy
        subsidy >>= 1
    return total


# --------------------------------------------------------------------------
# transactions
# --------------------------------------------------------------------------


@dataclass
class TxIn:
    txid: str
    vout: int
    pubkey: str = ""  # 33-byte compressed key, hex
    sig: str = ""  # 64-byte compact signature, hex

    def to_dict(self):
        return {"txid": self.txid, "vout": self.vout, "pubkey": self.pubkey, "sig": self.sig}

    @staticmethod
    def from_dict(d):
        d = _as_dict(d, "input")
        return TxIn(
            _as_str(d["txid"], "txid"),
            _as_int(d["vout"], "vout"),
            _as_str(d.get("pubkey", ""), "pubkey"),
            _as_str(d.get("sig", ""), "sig"),
        )


@dataclass
class TxOut:
    value: int  # in grains
    address: str

    def to_dict(self):
        return {"value": self.value, "address": self.address}

    @staticmethod
    def from_dict(d):
        d = _as_dict(d, "output")
        return TxOut(_as_int(d["value"], "value"), _as_str(d["address"], "address"))


@dataclass
class Tx:
    version: int = 1
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    locktime: int = 0
    coinbase: str = ""  # message, set only on the coinbase transaction
    cb_height: int = 0  # BIP 34: the coinbase commits to its own block height
    memo: str = ""  # note attached to a transfer, committed to by the signature

    @property
    def is_coinbase(self) -> bool:
        return not self.inputs

    def serialize_core(self) -> bytes:
        """
        Canonical serialisation used for both the txid and the signature hash.

        Signatures are excluded but public keys are committed to, so a
        transaction has exactly one txid regardless of how it was signed.
        This removes Bitcoin's pre-segwit malleability by construction.
        """
        parts = [f"v{self.version}"]
        if self.is_coinbase:
            parts.append(f"cb:{self.cb_height}:" + self.coinbase.encode().hex())
        else:
            for i in self.inputs:
                parts.append(f"in:{i.txid}:{i.vout}:{i.pubkey}")
            parts.append("memo:" + self.memo.encode().hex())
        for o in self.outputs:
            parts.append(f"out:{o.value}:{o.address}")
        parts.append(f"lt{self.locktime}")
        return "|".join(parts).encode()

    def txid(self) -> str:
        return crypto.sha256d(self.serialize_core()).hex()

    def sighash(self) -> bytes:
        """The digest every input signs. Identical to the txid preimage hash."""
        return crypto.sha256d(self.serialize_core())

    def to_dict(self):
        d = {
            "version": self.version,
            "inputs": [i.to_dict() for i in self.inputs],
            "outputs": [o.to_dict() for o in self.outputs],
            "locktime": self.locktime,
        }
        if self.is_coinbase:
            d["coinbase"] = self.coinbase
            d["cb_height"] = self.cb_height
        else:
            d["memo"] = self.memo
        return d

    @staticmethod
    def from_dict(d):
        d = _as_dict(d, "transaction")
        return Tx(
            version=_as_int(d.get("version", 1), "version"),
            inputs=[TxIn.from_dict(x) for x in _as_list(d.get("inputs", []), "inputs")],
            outputs=[TxOut.from_dict(x) for x in _as_list(d.get("outputs", []), "outputs")],
            locktime=_as_int(d.get("locktime", 0), "locktime"),
            coinbase=_as_str(d.get("coinbase", ""), "coinbase"),
            cb_height=_as_int(d.get("cb_height", 0), "cb_height"),
            memo=_as_str(d.get("memo", ""), "memo"),
        )


# --------------------------------------------------------------------------
# merkle tree
# --------------------------------------------------------------------------


def merkle_root(txids) -> str:
    """
    Bitcoin's merkle tree, including the odd-node duplication rule.

    Duplicate txids inside one block are rejected elsewhere, which closes
    CVE-2012-2459 (the duplication rule otherwise lets two distinct blocks
    share a merkle root).
    """
    if not txids:
        return "00" * 32
    layer = [bytes.fromhex(t) for t in txids]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [crypto.sha256d(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0].hex()


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------


@dataclass
class Block:
    height: int
    prev_hash: str
    merkle_root: str
    timestamp: int
    bits: int
    miner: str
    nonce: int = 0
    solution: str = ""  # 8-byte subset mask, hex
    algo: int = ALGO_SUBSET_SUM
    txs: list = field(default_factory=list)

    def header_core(self) -> bytes:
        """
        The bytes the puzzle is seeded from. Excludes the solution, since
        the solution is the answer to the puzzle these bytes define.

        `miner` is inside it, so each miner gets different puzzles and a
        solved block cannot be re-submitted by anyone else without redoing
        the work. `nonce` is inside it, so grinding changes the instance.
        """
        return (
            f"{self.height}|{self.prev_hash}|{self.merkle_root}|"
            f"{self.timestamp}|{self.bits:08x}|{self.miner}|{self.algo}|{self.nonce}"
        ).encode()

    def header(self) -> bytes:
        """Full header, including the solution, for block identity."""
        return self.header_core() + b"|" + self.solution.encode()

    def block_hash(self) -> str:
        return crypto.sha256d(self.header()).hex()

    def digest(self) -> bytes:
        return crypto.sha256d(self.header())

    def work(self) -> int:
        return target_to_work(bits_to_target(self.bits))

    def to_dict(self):
        return {
            "height": self.height,
            "prev_hash": self.prev_hash,
            "merkle_root": self.merkle_root,
            "timestamp": self.timestamp,
            "bits": self.bits,
            "miner": self.miner,
            "nonce": self.nonce,
            "algo": self.algo,
            "solution": self.solution,
            "txs": [t.to_dict() for t in self.txs],
        }

    @staticmethod
    def from_dict(d):
        d = _as_dict(d, "block")
        return Block(
            height=_as_int(d["height"], "height"),
            prev_hash=_as_str(d["prev_hash"], "prev_hash"),
            merkle_root=_as_str(d["merkle_root"], "merkle_root"),
            timestamp=_as_int(d["timestamp"], "timestamp"),
            bits=_as_int(d["bits"], "bits"),
            miner=_as_str(d["miner"], "miner"),
            nonce=_as_int(d.get("nonce", 0), "nonce"),
            solution=_as_str(d.get("solution", ""), "solution"),
            algo=_as_int(d.get("algo", ALGO_SUBSET_SUM), "algo"),
            txs=[Tx.from_dict(t) for t in _as_list(d.get("txs", []), "txs")],
        )


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def check_pow(block: Block) -> None:
    """
    Verify the block's proof of work: the knapsack is solved, and the
    block hash is at or below the compact target. Constant time in
    difficulty — 44 additions and two SHA-256s at any height.
    """
    if block.algo != ALGO_SUBSET_SUM:
        raise ConsensusError(f"unknown work function: algo {block.algo}")
    if not isinstance(block.nonce, int) or not 0 <= block.nonce < powfn.MAX_NONCE:
        raise ConsensusError("nonce out of range")
    try:
        subset = powfn.decode_solution(block.solution)
    except ValueError as exc:
        raise ConsensusError(f"malformed solution: {exc}") from None
    core = block.header_core()
    if not powfn.check_subset(core, subset):
        raise ConsensusError("subset does not solve the header's knapsack")
    target = bits_to_target(block.bits)
    if not powfn.hash_meets_target(block.digest(), target):
        raise ConsensusError(
            f"block hash is above the target (bits {block.bits:#010x})"
        )


def check_miner_name(miner: str) -> None:
    if not miner or len(miner) > MAX_MINER_LEN:
        raise ConsensusError("miner name must be 1-39 characters")
    if not all(c.isalnum() or c == "-" for c in miner):
        raise ConsensusError("miner name must be alphanumeric or hyphen")


def check_message(msg: str, is_genesis: bool = False) -> None:
    cap = MAX_GENESIS_MESSAGE_BYTES if is_genesis else MAX_MESSAGE_BYTES
    if len(msg.encode()) > cap:
        raise ConsensusError(f"message is {len(msg.encode())} bytes, cap is {cap}")
    if any(ord(c) < 32 or ord(c) == 127 for c in msg):
        raise ConsensusError("message contains control characters")
    if "|" in msg:
        raise ConsensusError("message may not contain '|' (serialisation delimiter)")


def check_memo(memo: str) -> None:
    if len(memo.encode()) > MAX_MEMO_BYTES:
        raise ConsensusError(f"memo is {len(memo.encode())} bytes, cap is {MAX_MEMO_BYTES}")
    if any(ord(c) < 32 or ord(c) == 127 for c in memo):
        raise ConsensusError("memo contains control characters")
    if "|" in memo:
        raise ConsensusError("memo may not contain '|' (serialisation delimiter)")


def validate_tx_shape(tx: Tx) -> None:
    if tx.version != 1:
        raise ConsensusError("unsupported transaction version")
    if not tx.is_coinbase:
        check_memo(tx.memo)
    if len(tx.outputs) < 1 or len(tx.outputs) > MAX_OUTPUTS:
        raise ConsensusError(f"transaction must have 1-{MAX_OUTPUTS} outputs")
    if len(tx.inputs) > MAX_INPUTS:
        raise ConsensusError(f"transaction may have at most {MAX_INPUTS} inputs")
    for o in tx.outputs:
        if not isinstance(o.value, int) or o.value <= 0:
            raise ConsensusError("output values must be positive integers")
        if o.value > MAX_MONEY:
            raise ConsensusError("output value out of range")
        if not crypto.address_is_valid(o.address):
            raise ConsensusError(f"invalid address: {o.address}")
    seen = set()
    for i in tx.inputs:
        key = (i.txid, i.vout)
        if key in seen:
            raise ConsensusError("transaction spends the same outpoint twice")
        seen.add(key)
        if not _is_hex(i.txid, 64):
            raise ConsensusError("malformed input txid")
        if i.vout < 0:
            raise ConsensusError("negative output index")


class UTXOSet:
    """Unspent outputs, keyed by (txid, vout). Values carry maturity height."""

    def __init__(self):
        self.utxos = {}  # (txid, vout) -> {"value", "address", "height", "coinbase"}

    def copy(self):
        u = UTXOSet()
        u.utxos = dict(self.utxos)
        return u

    def add_tx(self, tx: Tx, height: int) -> None:
        txid = tx.txid()
        for idx, out in enumerate(tx.outputs):
            self.utxos[(txid, idx)] = {
                "value": out.value,
                "address": out.address,
                "height": height,
                "coinbase": tx.is_coinbase,
            }

    def spend(self, txid: str, vout: int) -> None:
        del self.utxos[(txid, vout)]

    def get(self, txid: str, vout: int):
        return self.utxos.get((txid, vout))

    def has_tx(self, txid: str) -> bool:
        """True if any output of this txid is still unspent (BIP 30 check)."""
        return any(t == txid for t, _ in self.utxos)

    def balances(self):
        out = {}
        for u in self.utxos.values():
            out[u["address"]] = out.get(u["address"], 0) + u["value"]
        return out

    def total(self) -> int:
        return sum(u["value"] for u in self.utxos.values())


def validate_tx(tx: Tx, utxos: UTXOSet, height: int) -> int:
    """
    Fully validate a non-coinbase transaction against the UTXO set.
    Returns the fee in grains.
    """
    validate_tx_shape(tx)
    if tx.is_coinbase:
        raise ConsensusError("coinbase transaction in non-coinbase position")

    digest = tx.sighash()
    total_in = 0

    for i in tx.inputs:
        entry = utxos.get(i.txid, i.vout)
        if entry is None:
            raise ConsensusError(f"input {i.txid[:12]}…:{i.vout} is unknown or already spent")
        if entry["coinbase"] and height - entry["height"] < COINBASE_MATURITY:
            raise ConsensusError(
                f"coinbase output not mature: needs {COINBASE_MATURITY} confirmations, "
                f"has {height - entry['height']}"
            )
        try:
            pub_bytes = bytes.fromhex(i.pubkey)
        except ValueError:
            raise ConsensusError("malformed public key")
        if crypto.pubkey_to_address(pub_bytes) != entry["address"]:
            raise ConsensusError(
                f"public key does not match the address holding {i.txid[:12]}…:{i.vout}"
            )
        try:
            sig_bytes = bytes.fromhex(i.sig)
        except ValueError:
            raise ConsensusError("malformed signature")
        if not crypto.verify(pub_bytes, digest, sig_bytes):
            raise ConsensusError(f"invalid signature on input {i.txid[:12]}…:{i.vout}")
        total_in += entry["value"]

    total_out = sum(o.value for o in tx.outputs)
    if total_out > total_in:
        raise ConsensusError(
            f"outputs ({total_out}) exceed inputs ({total_in})"
        )
    return total_in - total_out


def validate_block(
    block: Block,
    prev: "Block | None",
    utxos: UTXOSet,
    expected_bits: int,
    median_time_past: int,
    now: int,
) -> UTXOSet:
    """
    Validate a block in full and return the UTXO set that results from it.
    The passed-in set is not modified.
    """
    check_miner_name(block.miner)

    if not (0 <= block.bits <= 0xFFFFFFFF):
        raise ConsensusError("bits out of range")
    if block.timestamp < 0:
        raise ConsensusError("timestamp is negative")
    if not _is_hex(block.prev_hash, 64):
        raise ConsensusError("malformed prev_hash")
    if not _is_hex(block.merkle_root, 64):
        raise ConsensusError("malformed merkle root")

    if prev is None:
        if block.height != 0:
            raise ConsensusError("genesis block must be at height 0")
        if block.prev_hash != "00" * 32:
            raise ConsensusError("genesis block must have a null prev_hash")
    else:
        if block.height != prev.height + 1:
            raise ConsensusError(f"expected height {prev.height + 1}, got {block.height}")
        if block.prev_hash != prev.block_hash():
            raise ConsensusError(
                f"stale block: builds on {block.prev_hash[:16]}… but the tip is "
                f"{prev.block_hash()[:16]}…"
            )

    if block.bits != expected_bits:
        raise ConsensusError(
            f"wrong difficulty: expected bits {expected_bits:#010x}, got {block.bits:#010x}"
        )
    if block.timestamp > now + MAX_FUTURE_DRIFT:
        raise ConsensusError("timestamp is more than 2 hours in the future")
    if prev is not None and block.timestamp <= median_time_past:
        raise ConsensusError(
            f"timestamp {block.timestamp} is not after median-time-past {median_time_past}"
        )

    if not block.txs:
        raise ConsensusError("block has no transactions")
    if len(block.txs) > MAX_TXS_PER_BLOCK:
        raise ConsensusError(f"block exceeds {MAX_TXS_PER_BLOCK} transactions")
    if not block.txs[0].is_coinbase:
        raise ConsensusError("first transaction must be the coinbase")
    for t in block.txs[1:]:
        if t.is_coinbase:
            raise ConsensusError("only one coinbase transaction allowed")

    txids = [t.txid() for t in block.txs]
    if len(set(txids)) != len(txids):
        raise ConsensusError("duplicate transactions in block")
    if merkle_root(txids) != block.merkle_root:
        raise ConsensusError("merkle root does not commit to these transactions")

    check_pow(block)

    coinbase = block.txs[0]
    validate_tx_shape(coinbase)
    check_message(coinbase.coinbase, is_genesis=(block.height == 0))
    if coinbase.cb_height != block.height:
        raise ConsensusError(
            f"BIP 34: coinbase commits to height {coinbase.cb_height}, "
            f"block is at height {block.height}"
        )
    for txid in txids:
        if utxos.has_tx(txid):
            raise ConsensusError(f"BIP 30: transaction {txid[:16]}… already exists unspent")

    working = utxos.copy()
    fees = 0
    for t in block.txs[1:]:
        fees += validate_tx(t, working, block.height)
        for i in t.inputs:
            working.spend(i.txid, i.vout)
        working.add_tx(t, block.height)

    expected_reward = block_subsidy(block.height) + fees
    paid = sum(o.value for o in coinbase.outputs)
    if paid != expected_reward:
        raise ConsensusError(
            f"coinbase pays {paid} grains, expected exactly {expected_reward} "
            f"(subsidy {block_subsidy(block.height)} + fees {fees})"
        )

    working.add_tx(coinbase, block.height)
    return working


def median_time_past(blocks) -> int:
    """Median timestamp of the last MEDIAN_TIME_SPAN blocks, as Bitcoin defines it."""
    if not blocks:
        return 0
    recent = sorted(b.timestamp for b in blocks[-MEDIAN_TIME_SPAN:])
    return recent[len(recent) // 2]


def format_amount(grains: int) -> str:
    whole = grains // COIN
    frac = grains % COIN
    return f"{whole}.{frac:08d}"
