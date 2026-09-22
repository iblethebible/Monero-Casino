"""Place a wager and reserve its stake in one InnoDB transaction."""
from database import get_db_connection, retry_deadlocks
from money import parse_amount
from rounds import get_chain_tip, next_betting_height


@retry_deadlocks
def place_bet(user_id, amount_text, game_type, selection):
    amount = parse_amount(amount_text)
    if game_type == 'digits':
        if not isinstance(selection, str) or not selection.isascii() or not selection.isdigit() or not 0 <= int(selection) <= 99:
            raise ValueError('Choose a number from 00 to 99.')
        chosen_number, bet_value = int(selection), None
    elif game_type in ('odd_even', 'high_low'):
        allowed = ('odd', 'even') if game_type == 'odd_even' else ('high', 'low')
        if selection not in allowed:
            raise ValueError('Invalid selection.')
        chosen_number, bet_value = None, selection
    else:
        raise ValueError('Unknown game type.')

    height = next_betting_height(get_chain_tip())
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    try:
        # Unique block_height makes concurrent first wagers converge on one round.
        cur.execute('''INSERT INTO blocks (block_height, block_hash, last_two_digits)
                       VALUES (%s, 'pending', NULL)
                       ON DUPLICATE KEY UPDATE block_height = VALUES(block_height)''', (height,))
        cur.execute('SELECT * FROM blocks WHERE block_height = %s FOR UPDATE', (height,))
        block = cur.fetchone()
        if block['resolved_at'] is not None or block['block_hash'] not in ('pending', ''):
            raise ValueError('This round is closed. Please try the next round.')
        cur.execute('''SELECT w.id FROM wallets w JOIN users u ON u.wallet_id = w.id
                       WHERE u.id = %s AND w.user_id = u.id''', (user_id,))
        wallet = cur.fetchone()
        if not wallet:
            raise ValueError('No active wallet.')
        cur.execute('''UPDATE wallets SET virtual_balance = virtual_balance - %s
                       WHERE id = %s AND virtual_balance >= %s''', (amount, wallet['id'], amount))
        if cur.rowcount != 1:
            raise ValueError('Insufficient available balance.')
        # Recheck after BOTH round and wallet locks: a request may have waited
        # on either. A crossed cutoff rolls the provisional debit back too.
        if height != next_betting_height(get_chain_tip()):
            raise ValueError('The betting cutoff passed. Please try the next round.')
        cur.execute('''INSERT INTO bets
                       (user_id, block_id, bet_amount, chosen_number, bet_value, game_type, bet_status)
                       VALUES (%s,%s,%s,%s,%s,%s,'pending')''',
                    (user_id, block['id'], amount, chosen_number, bet_value, game_type))
        bet_id = cur.lastrowid
        conn.commit()
        return bet_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
