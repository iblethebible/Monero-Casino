"""Opt-in integration coverage for deposit observation and crediting.

The target database must already contain the baseline and safety migrations.
These tests only create and remove uniquely identifiable fixture rows.
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import threading
import unittest
import uuid
from unittest.mock import patch


ENABLED = os.getenv("CASINO_INTEGRATION_TESTS") == "1"
ATOMIC = 10**12


@unittest.skipUnless(ENABLED, "set CASINO_INTEGRATION_TESTS=1 for disposable-MySQL tests")
class DepositSafetyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import mysql.connector

        cls.mysql = mysql.connector
        global deposits
        import deposits

        name = os.environ.get("DB_NAME", "")
        if not name or not any(marker in name.lower() for marker in ("test", "qa")):
            raise unittest.SkipTest("DB_NAME must visibly identify a disposable test/QA database")
        conn = cls.connect()
        cur = conn.cursor()
        try:
            cur.execute("SHOW TABLES LIKE 'deposits'")
            if cur.fetchone() is None:
                raise unittest.SkipTest("apply baseline.sql and migrations before integration tests")
        finally:
            cur.close()
            conn.close()

    @classmethod
    def connect(cls):
        return cls.mysql.connect(
            host=os.getenv("DB_HOST", "127.0.0.1"),
            port=int(os.getenv("DB_PORT", "3306")),
            user=os.getenv("DB_USER", "casino_admin"),
            password=os.getenv("DB_PASSWORD", ""),
            database=os.environ["DB_NAME"],
            connection_timeout=10,
        )

    def setUp(self):
        marker = uuid.uuid4().hex.replace("0", "z")
        self.addresses = ["4" + marker + char * 62 for char in ("a", "b")]
        self.house_address = "8" + marker + "c" * 62
        self.user_ids = []
        self.wallet_ids = []
        self._seed_fixture(marker)

    def tearDown(self):
        conn = self.connect()
        cur = conn.cursor()
        try:
            if self.user_ids:
                placeholders = ",".join(["%s"] * len(self.user_ids))
                cur.execute(f"DELETE FROM deposits WHERE user_id IN ({placeholders})", self.user_ids)
                cur.execute(f"DELETE FROM transactions WHERE user_id IN ({placeholders})", self.user_ids)
            for wallet_id in self.wallet_ids:
                cur.execute("DELETE FROM wallets WHERE id=%s", (wallet_id,))
            for user_id in self.user_ids:
                cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
            cur.execute("DELETE FROM admin_wallets WHERE id=%s", (self.house_id,))
            conn.commit()
        finally:
            cur.close()
            conn.close()

    def _seed_fixture(self, marker):
        conn = self.connect()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO admin_wallets (wallet_address,balance,label) VALUES (%s,0,'house')",
                (self.house_address,),
            )
            self.house_id = cur.lastrowid
            for index, address in enumerate(self.addresses):
                cur.execute(
                    "INSERT INTO users (username,password_hash) VALUES (%s,'integration-test-only')",
                    (f"deposit_qa_{marker}_{index}",),
                )
                user_id = cur.lastrowid
                cur.execute(
                    "INSERT INTO wallets (user_id,address,balance,virtual_balance) VALUES (%s,%s,0,0)",
                    (user_id, address),
                )
                wallet_id = cur.lastrowid
                cur.execute("UPDATE users SET wallet_id=%s WHERE id=%s", (wallet_id, user_id))
                self.user_ids.append(user_id)
                self.wallet_ids.append(wallet_id)
            conn.commit()
        finally:
            cur.close()
            conn.close()

    def _row(self, sql, params=()):
        conn = self.connect()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params)
            return cur.fetchone()
        finally:
            cur.close()
            conn.close()

    def transfer(self, tx_hash, user=0, amount="1", height=111, confirmations=10,
                 locked=False, double_spend_seen=False, unlock_time=0):
        return {
            "txid": tx_hash,
            "address": self.addresses[user],
            "amount": int(Decimal(amount) * ATOMIC),
            "height": height,
            "confirmations": confirmations,
            "locked": locked,
            "unlock_time": unlock_time,
            "double_spend_seen": double_spend_seen,
            "subaddr_index": {"major": 0, "minor": user + 1},
        }

    def scan(self, transfers, tip=120, wallet_height=None, recorder=None, block_hashes=None):
        wallet_height = tip + 1 if wallet_height is None else wallet_height
        calls = recorder if recorder is not None else []

        def rpc(method, params=None):
            calls.append((method, params))
            if method == "get_height":
                return {"height": wallet_height}
            if method == "get_address":
                return {
                    "addresses": [
                        {"address": address, "address_index": index + 1}
                        for index, address in enumerate(self.addresses)
                    ]
                }
            if method == "get_transfers":
                return {"in": transfers}
            raise AssertionError("deposit scan attempted unexpected wallet RPC: " + method)

        def header(height):
            height = int(height)
            block_hash = (block_hashes or {}).get(height, f"{height:064x}")
            return {"height": height, "hash": block_hash}

        with patch.object(deposits.wallet_rpc, "call", side_effect=rpc), patch.object(
            deposits, "get_chain_tip", return_value=tip
        ), patch.object(deposits, "get_block_header", side_effect=header):
            return deposits.scan_deposits()

    def assert_wallet(self, index, balance, held=False):
        row = self._row(
            "SELECT balance,virtual_balance,deposit_hold FROM wallets WHERE id=%s",
            (self.wallet_ids[index],),
        )
        self.assertEqual(Decimal(row["balance"]), Decimal(balance))
        self.assertEqual(Decimal(row["virtual_balance"]), Decimal(balance))
        self.assertEqual(bool(row["deposit_hold"]), held)

    def test_mature_deposit_replay_credits_exactly_once_and_never_sweeps(self):
        tx_hash = "1" * 64
        calls = []
        transfer = self.transfer(tx_hash, amount="1.25")
        self.scan([transfer], recorder=calls)
        self.scan([transfer], recorder=calls)

        self.assert_wallet(0, "1.250000000000")
        deposit = self._row(
            "SELECT state,COUNT(*) count FROM deposits WHERE tx_hash=%s GROUP BY state",
            (tx_hash,),
        )
        self.assertEqual(deposit, {"state": "credited", "count": 1})
        ledger = self._row(
            "SELECT COUNT(*) count FROM transactions WHERE user_id=%s AND tx_type='deposit'",
            (self.user_ids[0],),
        )
        self.assertEqual(ledger["count"], 1)
        house = self._row("SELECT balance FROM admin_wallets WHERE id=%s", (self.house_id,))
        self.assertEqual(Decimal(house["balance"]), Decimal("1.250000000000"))
        self.assertFalse(any(method.startswith("sweep") or method == "transfer" for method, _ in calls))

    def test_two_outputs_with_same_transaction_hash_credit_correct_users(self):
        tx_hash = "2" * 64
        self.scan([
            self.transfer(tx_hash, user=0, amount="0.4"),
            self.transfer(tx_hash, user=1, amount="0.6"),
        ])

        self.assert_wallet(0, "0.400000000000")
        self.assert_wallet(1, "0.600000000000")
        row = self._row(
            "SELECT COUNT(*) count,COUNT(DISTINCT transaction_id) ledger_count "
            "FROM deposits WHERE tx_hash=%s AND state='credited'",
            (tx_hash,),
        )
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["ledger_count"], 2)

    def test_immature_and_locked_outputs_are_not_credited(self):
        self.scan([
            self.transfer("3" * 64, amount="0.5", height=119, confirmations=2),
            self.transfer("4" * 64, amount="0.7", locked=True),
        ])

        self.assert_wallet(0, "0.000000000000")
        ledger = self._row(
            "SELECT COUNT(*) count FROM transactions WHERE user_id=%s AND tx_type='deposit'",
            (self.user_ids[0],),
        )
        self.assertEqual(ledger["count"], 0)

    def test_concurrent_scanners_credit_one_time(self):
        transfer = self.transfer("5" * 64, amount="0.8")
        first_in_snapshot = threading.Event()
        release_first = threading.Event()
        height_calls = 0
        height_lock = threading.Lock()

        def rpc(method, params=None):
            nonlocal height_calls
            if method == "get_height":
                with height_lock:
                    height_calls += 1
                    first_height_call = height_calls == 1
                if first_height_call:
                    first_in_snapshot.set()
                    self.assertTrue(release_first.wait(5), "test failed to release scanner")
                return {"height": 121}
            if method == "get_address":
                return {"addresses": [
                    {"address": address, "address_index": index + 1}
                    for index, address in enumerate(self.addresses)
                ]}
            if method == "get_transfers":
                return {"in": [transfer]}
            raise AssertionError("deposit scan attempted unexpected wallet RPC: " + method)

        def header(height):
            return {"height": int(height), "hash": f"{int(height):064x}"}

        with patch.object(deposits.wallet_rpc, "call", side_effect=rpc), patch.object(
            deposits, "get_chain_tip", return_value=120
        ), patch.object(deposits, "get_block_header", side_effect=header):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(deposits.scan_deposits)
                self.assertTrue(first_in_snapshot.wait(5), "first scanner did not acquire the lock")
                second = executor.submit(deposits.scan_deposits)
                second_result = second.result(timeout=5)
                release_first.set()
                first_result = first.result(timeout=5)
        results = [first_result, second_result]

        self.assert_wallet(0, "0.800000000000")
        ledger = self._row(
            "SELECT COUNT(*) count FROM transactions WHERE user_id=%s AND tx_type='deposit'",
            (self.user_ids[0],),
        )
        self.assertEqual(ledger["count"], 1)
        self.assertTrue(any(result.get("skipped") is True for result in results))

    def test_incomplete_wallet_snapshot_does_not_orphan_observation(self):
        tx_hash = "6" * 64
        self.scan([self.transfer(tx_hash, height=119, confirmations=2)])
        with self.assertRaises(Exception):
            self.scan([], wallet_height=120)

        row = self._row("SELECT state FROM deposits WHERE tx_hash=%s", (tx_hash,))
        self.assertEqual(row["state"], "observed")

    def test_malformed_snapshot_does_not_orphan_observation(self):
        tx_hash = "7" * 64
        self.scan([self.transfer(tx_hash, height=119, confirmations=2)])
        malformed = self.transfer("8" * 64)
        malformed["amount"] = "not-an-atomic-integer"
        with self.assertRaises(Exception):
            self.scan([malformed])

        row = self._row("SELECT state FROM deposits WHERE tx_hash=%s", (tx_hash,))
        self.assertEqual(row["state"], "observed")

    def test_uncredited_orphan_can_reappear_and_credit_when_mature(self):
        tx_hash = "9" * 64
        self.scan([self.transfer(tx_hash, height=119, confirmations=2)])
        self.scan([])
        orphan = self._row("SELECT state FROM deposits WHERE tx_hash=%s", (tx_hash,))
        self.assertEqual(orphan["state"], "orphaned")

        self.scan([self.transfer(tx_hash, height=119, confirmations=10)], tip=128)
        self.assert_wallet(0, "1.000000000000")
        restored = self._row("SELECT state FROM deposits WHERE tx_hash=%s", (tx_hash,))
        self.assertEqual(restored["state"], "credited")

    def test_credited_output_disappearing_places_hold_without_recredit(self):
        tx_hash = "a" * 64
        transfer = self.transfer(tx_hash, amount="0.9")
        self.scan([transfer])
        self.scan([])

        self.assert_wallet(0, "0.900000000000", held=True)
        row = self._row("SELECT state FROM deposits WHERE tx_hash=%s", (tx_hash,))
        self.assertEqual(row["state"], "review")

        self.scan([transfer])
        self.assert_wallet(0, "0.900000000000", held=True)
        ledger = self._row(
            "SELECT COUNT(*) count FROM transactions WHERE user_id=%s AND tx_type='deposit'",
            (self.user_ids[0],),
        )
        self.assertEqual(ledger["count"], 1)

    def test_canonical_block_change_quarantines_credited_output(self):
        tx_hash = "b" * 64
        transfer = self.transfer(tx_hash, amount="0.3")
        self.scan([transfer])
        self.scan([transfer], block_hashes={111: "f" * 64})

        self.assert_wallet(0, "0.300000000000", held=True)
        row = self._row("SELECT state,block_hash FROM deposits WHERE tx_hash=%s", (tx_hash,))
        self.assertEqual(row["state"], "review")
        ledger = self._row(
            "SELECT COUNT(*) count FROM transactions WHERE user_id=%s AND tx_type='deposit'",
            (self.user_ids[0],),
        )
        self.assertEqual(ledger["count"], 1)

    def test_legacy_ledger_hash_is_quarantined_without_double_credit(self):
        tx_hash = "c" * 64
        conn = self.connect()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO transactions (user_id,tx_hash,amount,tx_type) VALUES (%s,%s,1,'deposit')",
                (self.user_ids[0], tx_hash),
            )
            conn.commit()
        finally:
            cur.close()
            conn.close()

        self.scan([self.transfer(tx_hash)])

        self.assert_wallet(0, "0.000000000000", held=True)
        deposit = self._row("SELECT state,transaction_id FROM deposits WHERE tx_hash=%s", (tx_hash,))
        self.assertEqual(deposit["state"], "review")
        self.assertIsNone(deposit["transaction_id"])
        ledger = self._row(
            "SELECT COUNT(*) count FROM transactions WHERE user_id=%s AND tx_type='deposit'",
            (self.user_ids[0],),
        )
        self.assertEqual(ledger["count"], 1)


if __name__ == "__main__":
    unittest.main()
