"""Settle confirmed betting rounds atomically and idempotently."""

from decimal import Decimal, ROUND_DOWN

from database import get_db_connection
from rounds import canonical_outcome, get_block_header, get_chain_tip, is_confirmed


def _payout(bet, outcome):
    amount = Decimal(bet["bet_amount"])
    if bet["game_type"] == "digits":
        won, multiplier = int(bet["chosen_number"]) == outcome, Decimal("98")
    elif bet["game_type"] == "odd_even":
        won = bet["bet_value"] == ("even" if outcome % 2 == 0 else "odd")
        multiplier = Decimal("1.94")
    elif bet["game_type"] == "high_low":
        won = bet["bet_value"] == ("high" if outcome >= 50 else "low")
        multiplier = Decimal("1.94")
    else:
        raise ValueError(f"unsupported game type {bet['game_type']!r}")
    payout = amount * multiplier if won else Decimal("0")
    return won, payout.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)


def settle_round(conn, candidate, tip, header=None):
    """Settle one confirmed round. Return False if it is no longer eligible."""
    height = int(candidate["block_height"])
    if not is_confirmed(height, tip):
        return False
    # Injected headers support deterministic tests. Production fetches after the
    # row lock, so a worker that waited behind another never uses stale data.
    if header is not None:
        if int(header["height"]) != height:
            return False
        canonical_hash = header["hash"]
        outcome = canonical_outcome(canonical_hash)
    cursor = conn.cursor(dictionary=True)
    try:
        conn.start_transaction()
        cursor.execute("SELECT id, block_height, resolved_at FROM blocks WHERE id = %s FOR UPDATE",
                       (candidate["id"],))
        block = cursor.fetchone()
        if not block or block["resolved_at"] is not None or int(block["block_height"]) != height:
            conn.rollback()
            return False
        if header is None:
            fresh_header = get_block_header(height)
            if int(fresh_header["height"]) != height:
                raise RuntimeError("daemon returned a header for the wrong round")
            canonical_hash = fresh_header["hash"]
            outcome = canonical_outcome(canonical_hash)
        cursor.execute(
            """SELECT b.*, w.id AS wallet_id FROM bets b
               JOIN users u ON b.user_id = u.id
               LEFT JOIN wallets w ON u.wallet_id = w.id AND w.user_id = b.user_id
               WHERE b.block_id = %s AND b.bet_status = 'pending' FOR UPDATE""", (block["id"],))
        bets = cursor.fetchall()
        if any(bet["wallet_id"] is None for bet in bets):
            raise RuntimeError("pending bet has no wallet owned by its user")
        total_payout = Decimal("0")
        for bet in bets:
            won, payout = _payout(bet, outcome)
            cursor.execute("UPDATE bets SET bet_status = %s, payout = %s WHERE id = %s AND bet_status = 'pending'",
                           ("won" if won else "lost", payout, bet["id"]))
            if cursor.rowcount != 1:
                raise RuntimeError(f"bet {bet['id']} changed during settlement")
            if won:
                cursor.execute("UPDATE wallets SET virtual_balance = virtual_balance + %s WHERE id = %s",
                               (payout, bet["wallet_id"]))
                if cursor.rowcount != 1:
                    raise RuntimeError(f"wallet {bet['wallet_id']} is missing")
                cursor.execute(
                    """INSERT INTO transactions (user_id, tx_hash, amount, tx_type, created_at)
                       VALUES (%s, %s, %s, 'win_payout', NOW())""",
                    (bet["user_id"], f"win_payout_{bet['id']}", payout))
                total_payout += payout
        if total_payout:
            cursor.execute("SELECT id FROM admin_wallets WHERE label = 'house' ORDER BY id LIMIT 1 FOR UPDATE")
            house = cursor.fetchone()
            if not house:
                raise RuntimeError("house admin wallet is not configured")
            cursor.execute("UPDATE admin_wallets SET balance = balance - %s WHERE id = %s",
                           (total_payout, house["id"]))
        cursor.execute(
            """UPDATE blocks SET block_hash = %s, last_two_digits = %s, resolved_at = NOW()
               WHERE id = %s AND resolved_at IS NULL""", (canonical_hash, outcome, block["id"]))
        if cursor.rowcount != 1:
            raise RuntimeError("round changed during settlement")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def resolve_all_unresolved_blocks():
    tip = get_chain_tip()
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, block_height FROM blocks WHERE resolved_at IS NULL ORDER BY block_height")
        candidates = cursor.fetchall()
    finally:
        cursor.close()
        # The candidate SELECT starts an implicit MySQL transaction. End it before
        # settle_round starts a deliberate locking transaction on this connection.
        conn.rollback()
    settled = 0
    try:
        for candidate in candidates:
            height = int(candidate["block_height"])
            if is_confirmed(height, tip):
                settled += bool(settle_round(conn, candidate, tip))
    finally:
        conn.close()
    return settled


if __name__ == "__main__":
    print(f"Settled {resolve_all_unresolved_blocks()} confirmed round(s).")
