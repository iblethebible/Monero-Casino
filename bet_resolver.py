import mysql.connector
from decimal import Decimal

print("🔍 Starting bet resolver...")

def resolve_all_unresolved_blocks():
    conn = mysql.connector.connect(
        host="192.168.1.180",
        user="casino_admin",
        password="",
        database="monero_casino",
        ssl_disabled=True
    )
    cursor = conn.cursor(dictionary=True)

    # ✅ Fetch all unresolved blocks
    cursor.execute("SELECT id, block_height, last_two_digits FROM blocks WHERE resolved_at IS NULL")
    blocks = cursor.fetchall()

    if not blocks:
        print("ℹ️ No unresolved blocks found.")
        return

    for block in blocks:
        block_id = block["id"]
        block_height = block["block_height"]
        resolved_number = int(block["last_two_digits"])

        print(f"🧠 Resolving bets for block {block_height} (last_two_digits: {resolved_number})")

        # ✅ Fetch pending bets for this block
        cursor.execute("""
            SELECT b.*, w.id as wallet_id, u.admin_wallet_id
            FROM bets b
            JOIN users u ON b.user_id = u.id
            JOIN wallets w ON u.wallet_id = w.id
            WHERE b.block_id = %s AND b.bet_status = 'pending'
        """, (block_id,))
        bets = cursor.fetchall()

        for bet in bets:
            won = False
            payout = Decimal("0")
            bet_amount = Decimal(bet["bet_amount"])

            if bet["game_type"] == "digits":
                if int(bet["chosen_number"]) == resolved_number:
                    won = True
                    payout = bet_amount * Decimal("98")

            elif bet["game_type"] == "odd_even":
                is_even = resolved_number % 2 == 0
                if (bet["bet_value"] == "even" and is_even) or (bet["bet_value"] == "odd" and not is_even):
                    won = True
                    payout = bet_amount * Decimal("1.94")

            elif bet["game_type"] == "high_low":
                is_high = resolved_number >= 50
                if (bet["bet_value"] == "high" and is_high) or (bet["bet_value"] == "low" and not is_high):
                    won = True
                    payout = bet_amount * Decimal("1.94")

            if won:
                cursor.execute("""
                    UPDATE bets SET bet_status = 'won', payout = %s WHERE id = %s
                """, (payout, bet["id"]))
                cursor.execute("""
                    UPDATE wallets SET virtual_balance = virtual_balance + %s WHERE id = %s
                """, (payout, bet["wallet_id"]))
                cursor.execute("""
                    INSERT INTO transactions (user_id, tx_hash, amount, tx_type, created_at)
                    VALUES (%s, %s, %s, 'win_payout', NOW())
                """, (bet["user_id"], f"win_payout_{bet['id']}", payout))
                cursor.execute("""
                    UPDATE admin_wallets SET balance = balance - %s WHERE id = %s
                """, (payout, bet["admin_wallet_id"]))
            else:
                cursor.execute("""
                    UPDATE bets SET bet_status = 'lost', payout = 0 WHERE id = %s
                """, (bet["id"],))

        # ✅ Mark block as resolved
        cursor.execute("""
            UPDATE blocks SET resolved_at = NOW() WHERE id = %s
        """, (block_id,))

    conn.commit()
    cursor.close()
    conn.close()
    print("✅ All unresolved blocks have been processed.")

if __name__ == "__main__":
    resolve_all_unresolved_blocks()


##