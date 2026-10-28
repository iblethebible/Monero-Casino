import unittest
from unittest.mock import Mock, patch

import betting


class BettingTests(unittest.TestCase):
    def test_cutoff_crossing_while_waiting_for_wallet_rolls_back_debit(self):
        conn, cur = Mock(), Mock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [
            {'id': 1, 'resolved_at': None, 'block_hash': 'pending'},
            {'id': 2, 'deposit_hold': 0},
        ]
        cur.rowcount = 1
        with patch.object(betting, 'get_db_connection', return_value=conn), \
                patch.object(betting, 'get_chain_tip', side_effect=[101, 104]):
            with self.assertRaisesRegex(ValueError, 'cutoff'):
                betting.place_bet(7, '1', 'digits', '05')
        queries = [call.args[0] for call in cur.execute.call_args_list]
        self.assertTrue(any('UPDATE wallets' in sql for sql in queries))
        self.assertFalse(any('INSERT INTO bets' in sql for sql in queries))
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()

    def test_resolved_round_cannot_accept_another_bet(self):
        conn, cur = Mock(), Mock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = {'id': 1, 'resolved_at': '2026-09-22', 'block_hash': 'pending'}
        with patch.object(betting, 'get_db_connection', return_value=conn), \
                patch.object(betting, 'get_chain_tip', return_value=101):
            with self.assertRaisesRegex(ValueError, 'closed'):
                betting.place_bet(7, '1', 'digits', '05')
        self.assertFalse(any('UPDATE wallets' in call.args[0] for call in cur.execute.call_args_list))
        conn.rollback.assert_called_once()
