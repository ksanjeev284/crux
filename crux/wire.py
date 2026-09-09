"""
Compact wire format for CRUX submissions.

A block at any difficulty is a few hundred bytes plus its transactions.
The 24 KB cap is a policy limit so GitHub issue bodies (64 KB) and Actions
environment variables never see a payload that would overflow them.

    crux-block-v1:<base64(canonical json)>
    crux-tx-v1:<base64(canonical json)>
    crux-id-v1:<handle>:<pubkey>:<sig>
"""

import base64
import json

from .consensus import MAX_SUBMISSION_BYTES, Block, Tx

BLOCK_PREFIX = "crux-block-v1:"
TX_PREFIX = "crux-tx-v1:"
ID_PREFIX = "crux-id-v1:"


def encode_json(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()
    return base64.b64encode(raw).decode()


def decode_json(payload: str) -> dict:
    raw = base64.b64decode(payload, validate=True)
    if len(raw) > MAX_SUBMISSION_BYTES:
        raise ValueError(
            f"submission is {len(raw)} bytes; cap is {MAX_SUBMISSION_BYTES} "
            "so GitHub issue bodies and Actions never overflow"
        )
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    return data


def encode_block(block: Block) -> str:
    return BLOCK_PREFIX + encode_json(block.to_dict())


def encode_tx(tx: Tx) -> str:
    return TX_PREFIX + encode_json(tx.to_dict())


def find_payload(body: str):
    """Return (kind, payload) for the first CRUX line in `body`, or (None, None)."""
    for line in body.splitlines():
        line = line.strip().strip("`")
        if line.startswith(BLOCK_PREFIX):
            return "block", line[len(BLOCK_PREFIX):].strip()
        if line.startswith(TX_PREFIX):
            return "tx", line[len(TX_PREFIX):].strip()
        if line.startswith(ID_PREFIX):
            return "id", line[len(ID_PREFIX):].strip()
    return None, None
