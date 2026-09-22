import unittest
from decimal import Decimal
from unittest.mock import Mock

import bet_resolver


class SettlementTests(unittest.TestCase):
    def test_fractional_atomic_unit_payout_rounds_down(self):
        won, payout = bet_resolver._payout(
            {"bet_amount": Decimal("0.000000000001"), "game_type": "odd_even",
             "bet_value": "even"}, 12)
        self.assertTrue(won)
        self.assertEqual(payout, Decimal("0.000000000001"))

    def test_future_round_never_opens_a_transaction(self):
        connection = Mock()
        candidate = {"id": 7, "block_height": 100}
        header = {"height": 100, "hash": "1" * 64}
        self.assertFalse(bet_resolver.settle_round(connection, candidate, tip=99, header=header))
        connection.start_transaction.assert_not_called()

    def test_malformed_hash_never_opens_a_transaction(self):
        connection = Mock()
        candidate = {"id": 7, "block_height": 100}
        header = {"height": 100, "hash": "pending"}
        with self.assertRaises(ValueError):
            bet_resolver.settle_round(connection, candidate, tip=109, header=header)
        connection.start_transaction.assert_not_called()

    def test_resolved_round_is_idempotent(self):
        connection = Mock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = {"id": 7, "block_height": 100, "resolved_at": object()}
        candidate = {"id": 7, "block_height": 100}
        header = {"height": 100, "hash": "1" * 64}
        self.assertFalse(bet_resolver.settle_round(connection, candidate, tip=109, header=header))
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        self.assertIn("FOR UPDATE", cursor.execute.call_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main()
