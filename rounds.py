"""Canonical Monero round data and betting-height selection."""

import os
import time

import requests
from requests.auth import HTTPDigestAuth


class DaemonUnavailable(RuntimeError):
    pass


def _rpc_url():
    url = os.getenv("MONERO_DAEMON_RPC_URL", "http://127.0.0.1:18089")
    return url if url.rstrip("/").endswith("/json_rpc") else f"{url.rstrip('/')}/json_rpc"


def _rpc(method, params=None):
    auth = None
    user = os.getenv("MONERO_DAEMON_RPC_USER")
    if user:
        auth = HTTPDigestAuth(user, os.getenv("MONERO_DAEMON_RPC_PASSWORD", ""))
    response = requests.post(
        _rpc_url(), json={"jsonrpc": "2.0", "id": "casino", "method": method,
                          "params": params or {}}, auth=auth,
        timeout=float(os.getenv("MONERO_DAEMON_RPC_TIMEOUT", "10")))
    response.raise_for_status()
    body = response.json()
    if body.get("error") or "result" not in body:
        raise DaemonUnavailable(f"Monero daemon rejected {method}: {body.get('error')}")
    return body["result"]


def _assert_daemon_ready(info):
    if info.get("offline") is True or info.get("synchronized") is False:
        raise DaemonUnavailable("Monero daemon is not synchronized")
    height = int(info.get("height", 0))
    target = int(info.get("target_height", 0) or 0)
    if height <= 0 or (target and height < target):
        raise DaemonUnavailable("Monero daemon is still synchronizing")


def get_chain_tip():
    """Return the zero-based canonical tip, rejecting stale/unsynced nodes."""
    info = _rpc("get_info")
    _assert_daemon_ready(info)
    count = int(_rpc("get_block_count")["count"])
    if count <= 0 or abs(count - int(info["height"])) > 1:
        raise DaemonUnavailable("Monero daemon returned inconsistent chain heights")
    tip = count - 1
    tip_header = get_block_header(tip)
    timestamp = int(tip_header.get("timestamp", 0) or 0)
    max_age = int(os.getenv("MONERO_DAEMON_MAX_TIP_AGE_SECONDS", "1200"))
    if timestamp <= 0 or time.time() - timestamp > max_age:
        raise DaemonUnavailable("Monero daemon tip is stale")
    return tip


def get_block_header(height):
    header = (_rpc("get_block_header_by_height", {"height": int(height)}).get("block_header") or {})
    block_hash = str(header.get("hash", "")).lower()
    if int(header.get("height", -1)) != int(height):
        raise DaemonUnavailable("Monero daemon returned a header for the wrong height")
    if len(block_hash) != 64 or any(c not in "0123456789abcdef" for c in block_hash):
        raise DaemonUnavailable("Monero daemon returned an invalid block hash")
    return {**header, "hash": block_hash, "height": int(height)}


def canonical_outcome(block_hash):
    """Apply the existing game rule only to a verified hash."""
    value = str(block_hash).lower()
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("outcome requires a canonical 64-character hexadecimal hash")
    digits = "".join(c for c in value if c.isdigit())
    return int(digits[-2:]) if len(digits) >= 2 else 0


def min_confirmations():
    return max(1, int(os.getenv("MONERO_BET_MIN_CONFIRMATIONS", "10")))


def is_confirmed(height, tip, confirmations=None):
    required = min_confirmations() if confirmations is None else int(confirmations)
    return int(tip) - int(height) + 1 >= required


def next_betting_height(tip, cutoff_blocks=None):
    cutoff = max(2, int(cutoff_blocks or os.getenv("MONERO_BET_CUTOFF_BLOCKS", "2")))
    earliest = int(tip) + cutoff
    return earliest + (-earliest % 5)
