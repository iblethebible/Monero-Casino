"""Full-snapshot deposit reconciliation. This module never spends wallet funds."""
import os
import re
import uuid
from decimal import Decimal
from database import get_db_connection
from money import ATOMIC, parse_amount
from rounds import get_chain_tip, get_block_header
import wallet_rpc


class SnapshotError(RuntimeError):
    pass


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise SnapshotError('Invalid integer in wallet snapshot')
    return value


def _addresses(result):
    values = result.get('addresses')
    if not isinstance(values, list) or not values:
        raise SnapshotError('Missing complete wallet address list')
    addresses = {}
    for item in values:
        index = _integer(item.get('address_index'))
        address = item.get('address')
        if not isinstance(address, str) or not address or index in addresses:
            raise SnapshotError('Ambiguous address mapping')
        addresses[index] = address
    if len(set(addresses.values())) != len(addresses):
        raise SnapshotError('Duplicate address mapping')
    return addresses


def normalize_transfers(transfers, addresses):
    """The RPC amount already aggregates component amounts for one recipient.

    Ambiguous duplicate recipient rows abort the scan instead of being summed.
    """
    if not isinstance(transfers, list):
        raise SnapshotError('Missing complete incoming transfer list')
    normalized = {}
    for tx in transfers:
        if not isinstance(tx, dict):
            raise SnapshotError('Malformed transfer')
        tx_hash = tx.get('txid')
        if not isinstance(tx_hash, str) or not re.fullmatch('[0-9a-fA-F]{64}', tx_hash):
            raise SnapshotError('Invalid transaction hash')
        index = tx.get('subaddr_index')
        if not isinstance(index, dict) or _integer(index.get('major')) != 0:
            raise SnapshotError('Unexpected account')
        minor = _integer(index.get('minor'))
        if minor not in addresses or addresses[minor] != tx.get('address'):
            raise SnapshotError('Recipient does not match the wallet address mapping')
        atomic = _integer(tx.get('amount'), 1)
        if 'amounts' in tx:
            if not isinstance(tx['amounts'], list) or sum(_integer(v, 1) for v in tx['amounts']) != atomic:
                raise SnapshotError('Inconsistent component amounts')
        if type(tx.get('locked')) is not bool or type(tx.get('double_spend_seen', False)) is not bool:
            raise SnapshotError('Missing or invalid lock status')
        key = (tx_hash.lower(), 0, minor)
        if key in normalized:
            raise SnapshotError('Duplicate recipient transfer')
        normalized[key] = dict(tx_hash=key[0], account_index=0, subaddress_index=minor,
            address=tx['address'], amount=parse_amount(Decimal(atomic) / ATOMIC),
            height=_integer(tx.get('height'), 1), confirmations=_integer(tx.get('confirmations')),
            locked=tx['locked'], double_spend_seen=tx.get('double_spend_seen', False),
            unlock_time=_integer(tx.get('unlock_time')))
    return [normalized[key] for key in sorted(normalized)]


def _signature(transfers):
    return [{k: v for k, v in tx.items() if k != 'confirmations'} for tx in transfers]


def _snapshot():
    tip = get_chain_tip()
    anchor = get_block_header(tip)['hash']
    if _integer(wallet_rpc.call('get_height').get('height')) < tip + 1:
        raise SnapshotError('Wallet is behind the daemon')
    addresses = _addresses(wallet_rpc.call('get_address', {'account_index': 0}))
    # No height or subaddress filters: absence has meaning only in a full snapshot.
    # Monero omits optional empty arrays. The RPC wrapper has already required
    # a valid result object; a present non-list `in` is still rejected.
    first = normalize_transfers(wallet_rpc.call('get_transfers', {'in': True, 'account_index': 0}).get('in', []), addresses)
    headers = {}
    for tx in first:
        if tx['height'] > tip:
            raise SnapshotError('Chain advanced during snapshot; retry')
        if tx['height'] not in headers:
            headers[tx['height']] = get_block_header(tx['height'])['hash']
    second = normalize_transfers(wallet_rpc.call('get_transfers', {'in': True, 'account_index': 0}).get('in', []), addresses)
    if _signature(first) != _signature(second):
        raise SnapshotError('Wallet snapshot changed during scan')
    if _integer(wallet_rpc.call('get_height').get('height')) < tip + 1:
        raise SnapshotError('Wallet lost synchronization')
    if get_block_header(tip)['hash'] != anchor:
        raise SnapshotError('Canonical chain changed during scan')
    minimum = int(os.environ.get('MONERO_DEPOSIT_MIN_CONFIRMATIONS', '10'))
    if minimum < 1:
        raise SnapshotError('Confirmation threshold must be positive')
    for before, after in zip(first, second):
        before['confirmations'] = min(before['confirmations'], after['confirmations'], tip - before['height'] + 1)
        before['block_hash'] = headers[before['height']]
        before['eligible'] = (before['confirmations'] >= minimum and not before['locked']
                              and not before['double_spend_seen'] and before['unlock_time'] == 0)
    return tip, first, addresses


def _review(cur, deposit):
    cur.execute("UPDATE deposits SET state='review' WHERE id=%s", (deposit['id'],))
    cur.execute('UPDATE wallets SET deposit_hold=1 WHERE id=%s', (deposit['wallet_id'],))


def _apply(cur, scan_id, transfers, addresses):
    cur.execute('''SELECT w.id,w.user_id,w.address FROM wallets w JOIN users u
                   ON u.wallet_id=w.id AND w.user_id=u.id ORDER BY w.id''')
    wallets = {row['address']: row for row in cur.fetchall()}
    if not set(wallets).issubset(set(addresses.values())):
        raise SnapshotError('Configured wallet does not own all registered addresses')
    eligible = []
    for tx in transfers:
        wallet = wallets.get(tx['address'])
        if wallet is None:
            continue  # House/change addresses have no customer liability.
        key = (tx['tx_hash'], tx['account_index'], tx['subaddress_index'])
        cur.execute('''SELECT * FROM deposits WHERE tx_hash=%s AND account_index=%s
                       AND subaddress_index=%s FOR UPDATE''', key)
        deposit = cur.fetchone()
        if deposit is None:
            cur.execute('''INSERT INTO deposits
                           (tx_hash,account_index,subaddress_index,wallet_id,user_id,amount,
                            block_height,block_hash,confirmations,last_seen_scan)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (*key, wallet['id'], wallet['user_id'], tx['amount'], tx['height'],
                         tx['block_hash'], tx['confirmations'], scan_id))
            cur.execute('SELECT * FROM deposits WHERE id=%s', (cur.lastrowid,))
            deposit = cur.fetchone()
        if deposit['credited_at'] is None:
            cur.execute("SELECT id FROM transactions WHERE tx_hash=%s AND tx_type='deposit' FOR UPDATE", (tx['tx_hash'],))
            if cur.fetchone():
                # Old hash-only records cannot establish recipient allocations.
                _review(cur, deposit)
                deposit['state'] = 'review'
        inconsistent = (deposit['wallet_id'] != wallet['id'] or deposit['user_id'] != wallet['user_id']
                        or deposit['amount'] != tx['amount'])
        changed_credit = deposit['credited_at'] is not None and (
            deposit['block_height'] != tx['height'] or deposit['block_hash'] != tx['block_hash'] or not tx['eligible'])
        if inconsistent or changed_credit or deposit['state'] == 'review':
            _review(cur, deposit)
            if inconsistent:
                cur.execute('UPDATE wallets SET deposit_hold=1 WHERE id=%s', (wallet['id'],))
            # Retain original credited amount/hash/height for reconciliation.
            cur.execute('UPDATE deposits SET last_seen_scan=%s WHERE id=%s', (scan_id, deposit['id']))
            continue
        cur.execute('''UPDATE deposits SET block_height=%s,block_hash=%s,confirmations=%s,
                       last_seen_scan=%s,state=IF(state='orphaned','observed',state) WHERE id=%s''',
                    (tx['height'], tx['block_hash'], tx['confirmations'], scan_id, deposit['id']))
        if tx['eligible'] and deposit['credited_at'] is None:
            eligible.append(deposit)

    cur.execute("""SELECT * FROM deposits WHERE account_index=0 AND
                   (last_seen_scan IS NULL OR last_seen_scan<>%s) AND state<>'orphaned' FOR UPDATE""", (scan_id,))
    for deposit in cur.fetchall():
        if deposit['credited_at'] is not None or deposit['state'] == 'review':
            _review(cur, deposit)
        else:
            cur.execute("UPDATE deposits SET state='orphaned' WHERE id=%s", (deposit['id'],))
    credited = 0
    # Detect holds before making any new credit spendable in this transaction.
    for deposit in eligible:
        cur.execute('SELECT deposit_hold FROM wallets WHERE id=%s FOR UPDATE', (deposit['wallet_id'],))
        wallet = cur.fetchone()
        if not wallet or wallet['deposit_hold']:
            continue
        cur.execute("SELECT id FROM admin_wallets WHERE label='house' FOR UPDATE")
        house = cur.fetchone()
        if not house:
            raise SnapshotError('House accounting row missing')
        cur.execute("""INSERT INTO transactions (user_id,tx_hash,amount,tx_type)
                       VALUES (%s,%s,%s,'deposit')""",
                    (deposit['user_id'], f"deposit:{deposit['id']}", deposit['amount']))
        transaction_id = cur.lastrowid
        cur.execute('''UPDATE wallets SET balance=COALESCE(balance,0)+%s,
                       virtual_balance=COALESCE(virtual_balance,0)+%s WHERE id=%s AND deposit_hold=0''',
                    (deposit['amount'], deposit['amount'], deposit['wallet_id']))
        if cur.rowcount != 1:
            raise SnapshotError('Wallet changed during credit')
        cur.execute('UPDATE admin_wallets SET balance=balance+%s WHERE id=%s', (deposit['amount'], house['id']))
        cur.execute("""UPDATE deposits SET state='credited',credited_at=NOW(),transaction_id=%s
                       WHERE id=%s AND credited_at IS NULL AND state='observed'""", (transaction_id, deposit['id']))
        if cur.rowcount != 1:
            raise SnapshotError('Deposit was already credited')
        credited += 1
    cur.execute('SELECT state,COUNT(*) AS count FROM deposits GROUP BY state')
    stats = {row['state']: row['count'] for row in cur.fetchall()}
    stats['new_credits'] = credited
    return stats


def scan_deposits():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)
    scan_id = None
    scanner_locked = money_locked = False
    try:
        cur.execute("SELECT GET_LOCK('casino.deposits.v1',0) AS acquired")
        scanner_locked = cur.fetchone()['acquired'] == 1
        if not scanner_locked:
            return {'skipped': True}
        # Owning the scanner lock proves prior running attempts have exited.
        cur.execute("""UPDATE deposit_scan_runs SET state='failed',error='Interrupted scan',
                       completed_at=NOW() WHERE state='running'""")
        scan_id = str(uuid.uuid4())
        cur.execute("INSERT INTO deposit_scan_runs (id,state,tip_height) VALUES (%s,'running',0)", (scan_id,))
        conn.commit()
        tip, transfers, addresses = _snapshot()
        # Shared with spending worker: install quarantine atomically vs sending.
        cur.execute("SELECT GET_LOCK('casino.withdrawals.v1',0) AS acquired")
        money_locked = cur.fetchone()['acquired'] == 1
        if not money_locked:
            raise SnapshotError('Withdrawal processor busy; retry scan')
        stats = _apply(cur, scan_id, transfers, addresses)
        cur.execute("""UPDATE deposit_scan_runs SET state='complete',tip_height=%s,
                       completed_at=NOW() WHERE id=%s""", (tip, scan_id))
        conn.commit()
        return stats
    except Exception as exc:
        conn.rollback()
        if scan_id:
            cur.execute("""UPDATE deposit_scan_runs SET state='failed',error=%s,
                           completed_at=NOW() WHERE id=%s AND state='running'""", (type(exc).__name__[:100], scan_id))
            conn.commit()
        raise
    finally:
        try:
            conn.rollback()
            for acquired, name in ((money_locked, 'casino.withdrawals.v1'), (scanner_locked, 'casino.deposits.v1')):
                if acquired:
                    cur.execute('SELECT RELEASE_LOCK(%s)', (name,))
                    cur.fetchone()
        finally:
            cur.close()
            conn.close()
