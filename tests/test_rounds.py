import time
import unittest
from unittest.mock import patch

import rounds


class Response:
    def __init__(self, result):
        self.result = result

    def raise_for_status(self):
        pass

    def json(self):
        return {"result": self.result}


class RoundTests(unittest.TestCase):
    def test_tip_is_count_minus_one_and_requires_ready_daemon(self):
        replies = [Response({"height": 101, "target_height": 101, "synchronized": True}),
                   Response({"count": 101}),
                   Response({"block_header": {"height": 100, "hash": "a" * 64,
                                                "timestamp": int(time.time())}})]
        with patch("rounds.requests.post", side_effect=replies):
            self.assertEqual(rounds.get_chain_tip(), 100)

    def test_unsynchronized_daemon_is_rejected(self):
        with patch("rounds.requests.post",
                   return_value=Response({"height": 90, "target_height": 100, "synchronized": False})):
            with self.assertRaises(rounds.DaemonUnavailable):
                rounds.get_chain_tip()

    def test_confirmation_boundary_and_cutoff(self):
        self.assertFalse(rounds.is_confirmed(91, 99, confirmations=10))
        self.assertTrue(rounds.is_confirmed(90, 99, confirmations=10))
        self.assertEqual(rounds.next_betting_height(100, cutoff_blocks=2), 105)
        self.assertEqual(rounds.next_betting_height(103, cutoff_blocks=2), 105)

    def test_outcome_rejects_placeholder_and_derives_from_hash(self):
        with self.assertRaises(ValueError):
            rounds.canonical_outcome("pending")
        self.assertEqual(rounds.canonical_outcome("a" * 60 + "1b2c"), 12)

    def test_header_must_match_requested_height(self):
        reply = Response({"block_header": {"height": 9, "hash": "a" * 64}})
        with patch("rounds.requests.post", return_value=reply):
            with self.assertRaises(rounds.DaemonUnavailable):
                rounds.get_block_header(10)


if __name__ == "__main__":
    unittest.main()
