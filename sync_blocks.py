"""Backfill canonical hashes for every unresolved, confirmed betting round."""

from database import get_db_connection
from rounds import canonical_outcome, get_block_header, get_chain_tip, is_confirmed


def sync_blocks():
    tip = get_chain_tip()
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)
    updated = 0
    try:
        cursor.execute("SELECT id, block_height FROM blocks WHERE resolved_at IS NULL ORDER BY block_height")
        for block in cursor.fetchall():
            height = int(block["block_height"])
            if not is_confirmed(height, tip):
                continue
            header = get_block_header(height)
            outcome = canonical_outcome(header["hash"])
            cursor.execute(
                """UPDATE blocks SET block_hash = %s, last_two_digits = %s
                   WHERE id = %s AND resolved_at IS NULL""", (header["hash"], outcome, block["id"]))
            updated += cursor.rowcount
        db.commit()
        return updated
    except Exception:
        db.rollback()
        raise
    finally:
        cursor.close()
        db.close()


if __name__ == "__main__":
    print(f"Synchronized {sync_blocks()} confirmed round(s).")
