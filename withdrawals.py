"""Durable withdrawal reservations and prepare/persist/relay recovery.

Never call transfer without do_not_relay. Once metadata is persisted, it is the
only transaction this request may relay. Ambiguous relay outcomes retain funds.
"""
import logging
import re
import uuid
from decimal import Decimal

from database import get_db_connection, retry_deadlocks
from money import parse_amount, atomic_units, ATOMIC
import wallet_rpc

log = logging.getLogger(__name__)


@retry_deadlocks
def reserve_withdrawal(user_id, kind, address, amount_text, request_id):
    if kind not in ('user', 'admin'):
        raise ValueError('Invalid withdrawal kind.')
    amount = parse_amount(amount_text, minimum=Decimal('0.05') if kind == 'user' else Decimal('0.000000000001'))
    try:
        request_id = str(uuid.UUID(request_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid request identifier. Reload the form.') from exc
    if not isinstance(address, str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{95}|[1-9A-HJ-NP-Za-km-z]{106}', address):
        raise ValueError('Invalid Monero address.')

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    try:
        # Serialize duplicate requests even before their reservation exists.
        cur.execute('SELECT id, wallet_id, is_admin FROM users WHERE id = %s FOR UPDATE', (user_id,))
        user = cur.fetchone()
        if not user or (kind == 'admin' and not user['is_admin']):
            raise ValueError('Withdrawal is not authorized.')
        cur.execute('''SELECT * FROM withdrawals WHERE user_id=%s AND kind=%s
                       AND request_id=%s FOR UPDATE''', (user_id, kind, request_id))
        previous = cur.fetchone()
        if previous:
            if previous['address'] != address or previous['amount'] != amount:
                raise ValueError('This request identifier was already used for different details.')
            conn.commit()
            return previous
        validation = wallet_rpc.call('validate_address', {'address': address, 'any_net_type': False, 'allow_openalias': False})
        if validation.get('valid') is not True:
            raise ValueError('Invalid address or wrong Monero network.')
        cur.execute("SELECT id FROM admin_wallets WHERE label='house'")
        house = cur.fetchone()
        if not house:
            raise ValueError('House wallet has not been configured.')
        wallet_id = None
        if kind == 'user':
            wallet_id = user['wallet_id']
            cur.execute('''UPDATE wallets SET virtual_balance=virtual_balance-%s
                           WHERE id=%s AND user_id=%s AND virtual_balance >= %s
                           AND deposit_hold=0''',
                        (amount, wallet_id, user_id, amount))
        else:
            cur.execute('SELECT id FROM wallets WHERE deposit_hold=1 LIMIT 1 FOR UPDATE')
            if cur.fetchone():
                raise ValueError('House withdrawals are paused while a deposit is under review.')
            cur.execute('''UPDATE admin_wallets SET balance=balance-%s
                           WHERE id=%s AND balance >= %s''', (amount, house['id'], amount))
        if cur.rowcount != 1:
            if kind == 'user':
                raise ValueError('Insufficient available balance or deposit review in progress.')
            raise ValueError('Insufficient available balance.')
        cur.execute('''INSERT INTO withdrawals
                       (request_id,user_id,kind,wallet_id,admin_wallet_id,address,amount)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                    (request_id, user_id, kind, wallet_id, house['id'], address, amount))
        withdrawal_id = cur.lastrowid
        cur.execute('SELECT * FROM withdrawals WHERE id=%s', (withdrawal_id,))
        row = cur.fetchone()
        conn.commit()  # Durable debit and request exist before any transfer operation.
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def _prepared_result(result, amount):
    tx_hash, metadata, fee = result.get('tx_hash'), result.get('tx_metadata'), result.get('fee')
    if (not isinstance(tx_hash, str) or not re.fullmatch('[0-9a-fA-F]{64}', tx_hash)
            or not isinstance(metadata, str) or not re.fullmatch('[0-9a-fA-F]+', metadata)
            or len(metadata) % 2 or len(metadata) > 4_000_000
            or type(fee) is not int or fee < 0
            or type(result.get('amount')) is not int or result['amount'] != atomic_units(amount)):
        raise ValueError('Invalid prepared transaction response.')
    return tx_hash.lower(), metadata, Decimal(fee) / ATOMIC


def _already_relayed(row):
    try:
        result = wallet_rpc.call('get_transfer_by_txid', {'txid': row['tx_hash'], 'account_index': 0})
    except wallet_rpc.WalletRPCError:
        return False
    transfers = result.get('transfers') or [result.get('transfer', {})]
    return any(t.get('txid') == row['tx_hash'] and t.get('type') in ('out', 'pending')
               and t.get('amount') == atomic_units(row['amount']) for t in transfers)


def process_withdrawal(withdrawal_id):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    locked = False
    try:
        # Serializes wallet transaction preparation across web and worker processes.
        cur.execute("SELECT GET_LOCK('casino.withdrawals.v1', 0) AS acquired")
        locked = cur.fetchone()['acquired'] == 1
        if not locked:
            cur.execute('SELECT * FROM withdrawals WHERE id=%s', (withdrawal_id,))
            return cur.fetchone()
        cur.execute('SELECT * FROM withdrawals WHERE id=%s FOR UPDATE', (withdrawal_id,))
        row = cur.fetchone()
        if not row or row['state'] in ('submitted', 'failed'):
            conn.commit()
            return row
        if row['kind'] == 'user':
            cur.execute('SELECT deposit_hold FROM wallets WHERE id=%s AND user_id=%s FOR UPDATE',
                        (row['wallet_id'], row['user_id']))
            owner = cur.fetchone()
            if not owner:
                raise RuntimeError('Reservation owner missing.')
            held = bool(owner['deposit_hold'])
        else:
            cur.execute('SELECT id FROM wallets WHERE deposit_hold=1 LIMIT 1 FOR UPDATE')
            held = cur.fetchone() is not None
        if held:
            # A known broadcast must still be finalized so accounting catches up.
            # Reserved rows and unbroadcast prepared rows retain their reservation.
            if row['state'] != 'prepared' or not _already_relayed(row):
                conn.commit()
                return row
            known = True
        else:
            known = False
        if row['state'] == 'reserved':
            # An unresolved signed transaction may own inputs needed by a new one.
            cur.execute("SELECT id FROM withdrawals WHERE state='prepared' AND id<>%s LIMIT 1", (withdrawal_id,))
            if cur.fetchone():
                conn.commit()
                return row
            try:
                result = wallet_rpc.call('transfer', {
                    'account_index': 0,
                    'destinations': [{'address': row['address'], 'amount': atomic_units(row['amount'])}],
                    'priority': 1, 'do_not_relay': True, 'get_tx_metadata': True,
                })
            except wallet_rpc.WalletRPCError:
                # A rejected prepare cannot have broadcast anything. Release once.
                if row['kind'] == 'user':
                    cur.execute('UPDATE wallets SET virtual_balance=virtual_balance+%s WHERE id=%s', (row['amount'], row['wallet_id']))
                else:
                    cur.execute('UPDATE admin_wallets SET balance=balance+%s WHERE id=%s', (row['amount'], row['admin_wallet_id']))
                if cur.rowcount != 1:
                    raise RuntimeError('Reservation owner missing.')
                cur.execute("UPDATE withdrawals SET state='failed',last_error='Wallet rejected preparation' WHERE id=%s", (withdrawal_id,))
                conn.commit()
                row['state'] = 'failed'
                return row
            tx_hash, metadata, fee = _prepared_result(result, row['amount'])
            cur.execute("""UPDATE withdrawals SET state='prepared',tx_hash=%s,tx_metadata=%s,
                           fee=%s,last_error=NULL WHERE id=%s""", (tx_hash, metadata, fee, withdrawal_id))
            conn.commit()  # MUST succeed before relay; failed/ambiguous commit never relays here.
            cur.execute('SELECT * FROM withdrawals WHERE id=%s FOR UPDATE', (withdrawal_id,))
            row = cur.fetchone()
            known = False
        elif not known:
            known = _already_relayed(row)
        if not known:
            result = wallet_rpc.call('relay_tx', {'hex': row['tx_metadata']})
            if result.get('tx_hash') != row['tx_hash']:
                raise RuntimeError('Unexpected relay transaction hash.')
        # Reservation already debited principal; finalize ledger exactly once.
        if row['kind'] == 'user':
            cur.execute("""INSERT INTO transactions (user_id,tx_hash,amount,tx_type)
                           VALUES (%s,%s,%s,'withdrawal')""", (row['user_id'], row['tx_hash'], row['amount']))
            house_debit = row['amount'] + row['fee']
        else:
            cur.execute("""INSERT INTO admin_transactions (admin_wallet_id,tx_hash,amount,tx_type)
                           VALUES (%s,%s,%s,'withdrawal')""", (row['admin_wallet_id'], row['tx_hash'], row['amount']))
            house_debit = row['fee']
        cur.execute('UPDATE admin_wallets SET balance=balance-%s WHERE id=%s', (house_debit, row['admin_wallet_id']))
        if cur.rowcount != 1 and house_debit != 0:
            raise RuntimeError('House wallet missing.')
        cur.execute("UPDATE withdrawals SET state='submitted',last_error=NULL WHERE id=%s", (withdrawal_id,))
        conn.commit()
        row['state'] = 'submitted'
        return row
    except Exception as exc:
        conn.rollback()
        # No refunds after a possibly broadcast transaction. A worker retries the
        # saved metadata, or recognizes the saved hash in wallet transfer history.
        log.warning('Withdrawal %s pending recovery (%s)', withdrawal_id, type(exc).__name__)
        try:
            cur.execute("UPDATE withdrawals SET last_error=%s WHERE id=%s AND state IN ('reserved','prepared')",
                        (type(exc).__name__[:100], withdrawal_id))
            conn.commit()
            cur.execute('SELECT * FROM withdrawals WHERE id=%s', (withdrawal_id,))
            return cur.fetchone()
        except Exception:
            conn.rollback()
            raise
    finally:
        try:
            conn.rollback()
            if locked:
                cur.execute("SELECT RELEASE_LOCK('casino.withdrawals.v1')")
                cur.fetchone()
        finally:
            cur.close()
            conn.close()


def process_pending():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    try:
        cur.execute("SELECT id FROM withdrawals WHERE state IN ('reserved','prepared') ORDER BY state='prepared' DESC,id LIMIT 100")
        pending = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    for row in pending:
        process_withdrawal(row['id'])


if __name__ == '__main__':
    process_pending()
