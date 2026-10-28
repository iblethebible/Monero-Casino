"""Bounded wallet RPC requests. Never log signed transaction metadata."""
import os
import requests
from requests.auth import HTTPDigestAuth


class WalletRPCError(RuntimeError):
    pass


def call(method, params=None):
    username = os.environ.get('MONERO_WALLET_RPC_USER')
    auth = HTTPDigestAuth(username, os.environ.get('MONERO_WALLET_RPC_PASSWORD', '')) if username else None
    response = requests.post(
        os.environ.get('MONERO_WALLET_RPC_URL', 'http://127.0.0.1:18083/json_rpc'),
        json={'jsonrpc': '2.0', 'id': 'casino', 'method': method, 'params': params or {}},
        auth=auth, timeout=(5, 30),
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('error') or not isinstance(payload.get('result'), dict):
        # RPC error text can contain private information. Keep errors out of UI/logs.
        raise WalletRPCError(f'Wallet RPC rejected {method}.')
    return payload['result']
