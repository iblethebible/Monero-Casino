import mysql.connector
import requests

REMOTE_MONERO_NODE = "http://127.0.0.1:18089/json_rpc"

def get_block_count():
    payload = {
        "jsonrpc": "2.0",
        "id": "0",
        "method": "get_block_count"
    }
    try:
        response = requests.post(REMOTE_MONERO_NODE, json=payload, timeout=10)
        return response.json()["result"]["count"]
    except Exception as e:
        print("❌ Failed to fetch block height:", e)
        return None

def get_block_hash(height):
    payload = {
        "jsonrpc": "2.0",
        "id": "0",
        "method": "get_block_header_by_height",
        "params": {"height": height}
    }
    try:
        response = requests.post(REMOTE_MONERO_NODE, json=payload, timeout=10)
        return response.json()["result"]["block_header"]["hash"]
    except Exception as e:
        print(f"❌ Could not get hash for block {height}: {e}")
        return None

def extract_last_two_digits(block_hash):
    digits = ''.join(filter(str.isdigit, block_hash))
    return int(digits[-2:]) if len(digits) >= 2 else 0

def sync_blocks():
    db = mysql.connector.connect(
        host="localhost",
        user="casino_admin",
        password="Bighead4548",
        database="monero_casino"
    )
    cursor = db.cursor()

    latest_height = get_block_count()
    if not latest_height:
        return

    for height in range(latest_height - 50, latest_height + 1):  # Scan the last 50 blocks
        if height % 5 not in [0, 5]:
            continue  # Only process blocks ending in 0 or 5

        cursor.execute("SELECT id, block_hash FROM blocks WHERE block_height = %s", (height,))
        row = cursor.fetchone()

        if not row:
            # Insert new block
            print(f"🆕 Adding new betting block {height}")
            block_hash = get_block_hash(height)
            if block_hash:
                last_digits = extract_last_two_digits(block_hash)
                cursor.execute("""
                    INSERT INTO blocks (block_height, block_hash, last_two_digits)
                    VALUES (%s, %s, %s)
                """, (height, block_hash, last_digits))
                db.commit()
        elif row[1] in (None, "", "pending"):
            # Update hash and result for existing block
            print(f"🔄 Updating unresolved block {height}")
            block_hash = get_block_hash(height)
            if block_hash:
                last_digits = extract_last_two_digits(block_hash)
                cursor.execute("""
                    UPDATE blocks
                    SET block_hash = %s, last_two_digits = %s
                    WHERE block_height = %s
                """, (block_hash, last_digits, height))
                db.commit()

if __name__ == "__main__":
    sync_blocks()

##