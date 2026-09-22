"""Opt-in tests against a disposable MySQL schema; never point these at real data."""

import os
import threading
import unittest
import uuid
from decimal import Decimal
from unittest.mock import patch

import mysql.connector

import bet_resolver
from bet_resolver import settle_round


ENABLED = os.getenv("CASINO_INTEGRATION_TESTS") == "1"
HASH_12 = "a" * 60 + "1b2c"


def connect():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"), port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "casino_admin"), password=os.getenv("DB_PASSWORD", ""),
        database=os.environ["DB_NAME"])


@unittest.skipUnless(ENABLED, "set CASINO_INTEGRATION_TESTS=1 for disposable-MySQL tests")
class SettlementIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = os.environ.get("DB_NAME", "")
        if not name or not any(marker in name.lower() for marker in ("test", "qa")):
            raise unittest.SkipTest("DB_NAME must visibly identify a disposable test/QA database")

    def setUp(self):
        self.conn = connect()
        cur = self.conn.cursor()
        self.height = 8_000_000 + (uuid.uuid4().int % 100_000)
        suffix = uuid.uuid4().hex[:12]
        self.house_id = self.user_id = self.wallet_id = None
        try:
            cur.execute("SELECT id FROM admin_wallets WHERE label = 'house'")
            if cur.fetchone() is not None:
                raise RuntimeError("disposable settlement fixture requires no pre-existing house row")
            cur.execute("INSERT INTO admin_wallets (wallet_address, balance, label) VALUES (%s, 1000, 'house')",
                        (f"house-{suffix}",))
            self.house_id = cur.lastrowid
            cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, 'test')", (f"settle_{suffix}",))
            self.user_id = cur.lastrowid
            cur.execute("INSERT INTO wallets (user_id, address, virtual_balance) VALUES (%s, %s, 1)",
                        (self.user_id, f"test-address-{suffix}"))
            self.wallet_id = cur.lastrowid
            cur.execute("UPDATE users SET wallet_id = %s WHERE id = %s", (self.wallet_id, self.user_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise
        finally:
            cur.close()

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM transactions WHERE user_id = %s", (self.user_id,))
        cur.execute("DELETE FROM bets WHERE user_id = %s", (self.user_id,))
        cur.execute("DELETE FROM blocks WHERE block_height = %s", (self.height,))
        cur.execute("UPDATE users SET wallet_id = NULL WHERE id = %s", (self.user_id,))
        cur.execute("DELETE FROM wallets WHERE id = %s", (self.wallet_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (self.user_id,))
        cur.execute("DELETE FROM admin_wallets WHERE id = %s", (self.house_id,))
        self.conn.commit()
        cur.close()
        self.conn.close()

    def _round(self, resolved=False, placeholder="pending"):
        cur = self.conn.cursor()
        cur.execute("""INSERT INTO blocks (block_height, block_hash, last_two_digits, resolved_at)
                       VALUES (%s, %s, 99, %s)""",
                    (self.height, placeholder, "2026-01-01" if resolved else None))
        block_id = cur.lastrowid
        cur.execute("""INSERT INTO bets
                       (user_id, block_id, bet_amount, chosen_number, game_type, bet_status)
                       VALUES (%s, %s, 0.1, 12, 'digits', 'pending')""", (self.user_id, block_id))
        bet_id = cur.lastrowid
        self.conn.commit()
        cur.close()
        return {"id": block_id, "block_height": self.height}, bet_id

    def test_confirmed_placeholder_uses_canonical_header_and_pays_once_concurrently(self):
        candidate, bet_id = self._round()
        header = {"height": self.height, "hash": HASH_12}
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker():
            connection = connect()
            try:
                barrier.wait()
                results.append(settle_round(connection, candidate, self.height + 9, header))
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(errors)
        self.assertCountEqual(results, [True, False])
        cur = self.conn.cursor()
        cur.execute("SELECT bet_status, payout FROM bets WHERE id = %s", (bet_id,))
        self.assertEqual(cur.fetchone(), ("won", Decimal("9.800000000000")))
        cur.execute("SELECT block_hash, last_two_digits FROM blocks WHERE id = %s", (candidate["id"],))
        self.assertEqual(cur.fetchone(), (HASH_12, 12))
        cur.execute("SELECT COUNT(*) FROM transactions WHERE tx_hash = %s", (f"win_payout_{bet_id}",))
        self.assertEqual(cur.fetchone()[0], 1)
        cur.close()

    def test_future_placeholder_is_not_paid(self):
        candidate, bet_id = self._round()
        self.assertFalse(settle_round(self.conn, candidate, self.height + 8,
                                      {"height": self.height, "hash": HASH_12}))
        cur = self.conn.cursor()
        cur.execute("SELECT bet_status FROM bets WHERE id = %s", (bet_id,))
        self.assertEqual(cur.fetchone()[0], "pending")
        cur.close()

    def test_already_resolved_round_is_denied(self):
        candidate, bet_id = self._round(resolved=True, placeholder=HASH_12)
        self.assertFalse(settle_round(self.conn, candidate, self.height + 9,
                                      {"height": self.height, "hash": HASH_12}))
        cur = self.conn.cursor()
        cur.execute("SELECT bet_status FROM bets WHERE id = %s", (bet_id,))
        self.assertEqual(cur.fetchone()[0], "pending")
        cur.close()

    def test_resolver_entrypoint_ends_candidate_query_transaction(self):
        # Keep the mocked tip below rounds left by other integration fixtures;
        # resolve_all_unresolved_blocks must ignore those future rows.
        self.height = 50
        candidate, bet_id = self._round()
        cur = self.conn.cursor()
        cur.execute("""SELECT COUNT(*) FROM blocks
                       WHERE resolved_at IS NULL AND id <> %s AND block_height <= %s""",
                    (candidate["id"], self.height))
        if cur.fetchone()[0]:
            cur.close()
            self.skipTest("entrypoint test requires no other eligible unresolved round")
        cur.close()
        with patch.object(bet_resolver, "get_chain_tip", return_value=self.height + 9), \
             patch.object(bet_resolver, "get_block_header",
                          return_value={"height": self.height, "hash": HASH_12}):
            self.assertEqual(bet_resolver.resolve_all_unresolved_blocks(), 1)
        # The guard SELECT established a repeatable-read snapshot on this
        # observer connection; read the worker's committed result afresh.
        self.conn.rollback()
        cur = self.conn.cursor()
        cur.execute("SELECT bet_status FROM bets WHERE id = %s", (bet_id,))
        self.assertEqual(cur.fetchone()[0], "won")
        cur.close()


if __name__ == "__main__":
    unittest.main()
