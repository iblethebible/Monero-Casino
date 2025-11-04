# ──────────────────────────────────────────────────────────
# 🔧 1. CONFIG: Set your own values below
# ───────────────────────────────────────────────────────

WALLET_RPC_URL = "http://localhost:18083/json_rpc"
WALLET_NAME = "master"
WALLET_PASSWORD = ""
HOUSE_WALLET_ADDRESS = "89sAdoUmh7mgb1RcQ36tQFDZ4s7DWndT7MMqvwm1gKLQ7zncDen48PS6daw5euEDDH6o8JHdjTrYtL43dc4ut3KoKk8yDRM"

DB_CONFIG = {
    "host": "localhost",
    "user": "casino_admin",
    "password": "",
    "database": "monero_casino",
    "ssl_disabled": True
}

from decimal import Decimal
import mysql.connector
import requests
from datetime import datetime, timezone

# ─────────────────────────────────────
# 🛀 Sweep function
# ─────────────────────────────────────

def rpc(method, params=None):
    payload = {
        "jsonrpc": "2.0",
        "id": "0",
        "method": method,
        "params": params or {}
    }
    response = requests.post(WALLET_RPC_URL, json=payload)
    response.raise_for_status()
    response_json = response.json()
    if "error" in response_json:
        print(f"❌ RPC Error: {response_json['error']}")
        raise Exception(response_json["error"]["message"])
    return response_json["result"]

def sweep_subaddress(index, user_id, amount, cursor):
    try:
        result = rpc("sweep_all", {
            "account_index": 0,
            "subaddr_indices": [index],
            "address": HOUSE_WALLET_ADDRESS,
            "priority": 1
        })
        tx_hashes = result.get("tx_hash_list", [])
        for tx_hash in tx_hashes:
            print(f"✅ Swept subaddress {index} to house wallet (tx: {tx_hash})")
            cursor.execute("""
                INSERT INTO transactions (user_id, tx_hash, amount, tx_type, created_at)
                VALUES (%s, %s, %s, 'sweep', %s)
            """, (user_id, tx_hash, amount, datetime.now(timezone.utc)))
            cursor.execute("""
                UPDATE admin_wallets SET balance = balance + %s WHERE label = 'house'
            """, (amount,))
            cursor.execute("""
                UPDATE wallets SET balance = 0 WHERE user_id = %s
            """, (user_id,))
    except Exception as e:
        print(f"⚠️ Sweep failed for subaddress {index}: {e}")

# ─────────────────────────────────────
# 📦 3. Sync real wallet balances to database (update only)
# ─────────────────────────────────────

def check_and_update_balances():
    db = mysql.connector.connect(**DB_CONFIG)
    cursor = db.cursor(dictionary=True)
    result = rpc("get_transfers", {"in": True, "account_index": 0})

    for tx in result.get("in", []):
        address = tx["address"]
        amount = Decimal(tx["amount"]) / Decimal(1e12)
        txid = tx["txid"]
        index_data = tx.get("subaddr_index", {})
        if isinstance(index_data, dict):
            subaddr_index = index_data.get("minor", 0)
        else:
            subaddr_index = index_data  # Already an int

        cursor.execute("SELECT id FROM transactions WHERE tx_hash = %s", (txid,))
        if cursor.fetchone():
            print(f"🔁 Transaction {txid} already recorded — skipping.")
            continue

        cursor.execute("SELECT id, user_id FROM wallets WHERE address = %s", (address,))
        wallet = cursor.fetchone()
        if not wallet:
            print(f"⛔️ Address {address} not found in DB — skipping.")
            continue

        wallet_id = wallet["id"]
        user_id = wallet["user_id"]
        print(f"✅ New deposit of {amount} XMR to {address} (tx: {txid})")

        cursor.execute("""
            UPDATE wallets SET balance = balance + %s, virtual_balance = virtual_balance + %s WHERE id = %s
        """, (amount, amount, wallet_id))

        cursor.execute("""
            INSERT INTO transactions (user_id, tx_hash, amount, tx_type, created_at)
            VALUES (%s, %s, %s, 'deposit', %s)
        """, (user_id, txid, amount, datetime.now(timezone.utc)))

        db.commit()

        print(f"🔁 Calling sweep on subaddr_index={subaddr_index} for user_id={user_id} amount={amount}")

        if isinstance(subaddr_index, int):
            sweep_subaddress(subaddr_index, user_id, amount, cursor)
            db.commit()

    cursor.close()
    db.close()

# ─────────────────────────────────────
# 🚀 4. Entry point
# ─────────────────────────────────────

if __name__ == "__main__":
    check_and_update_balances()


##