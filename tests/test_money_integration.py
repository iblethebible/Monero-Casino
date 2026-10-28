"""Opt-in integration tests for the critical money transaction boundaries.

Run only against a disposable database whose baseline and migrations have
already been applied::

    CASINO_INTEGRATION_TESTS=1 DB_NAME=casino_critical_qa python -m unittest \
        tests.test_money_integration -v

The tests create and remove uniquely named fixture rows. They never create,
drop, truncate, or migrate database objects.
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import threading
import unittest
import uuid
from unittest.mock import patch


ENABLED = os.getenv("CASINO_INTEGRATION_TESTS") == "1"


@unittest.skipUnless(ENABLED, "set CASINO_INTEGRATION_TESTS=1 for disposable-MySQL tests")
class MoneySafetyIntegrationTests(unittest.TestCase):
    INITIAL_BALANCE = Decimal("1.000000000000")
    ATTEMPT_AMOUNT = Decimal("0.200000000000")

    @classmethod
    def setUpClass(cls):
        import mysql.connector

        cls.mysql = mysql.connector
        # Importing here keeps the default unit-test run independent of the
        # optional MySQL and wallet RPC dependencies.
        global betting, withdrawals
        import betting
        import withdrawals

        cls._assert_dedicated_database()

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

    @classmethod
    def _assert_dedicated_database(cls):
        name = os.environ.get("DB_NAME", "")
        if not name or not any(marker in name.lower() for marker in ("test", "qa")):
            raise unittest.SkipTest("DB_NAME must visibly identify a disposable test/QA database")
        conn = cls.connect()
        cur = conn.cursor()
        try:
            cur.execute("SHOW TABLES LIKE 'withdrawals'")
            if cur.fetchone() is None:
                raise unittest.SkipTest("apply baseline.sql and migrations before integration tests")
        finally:
            cur.close()
            conn.close()

    def setUp(self):
        self.marker = uuid.uuid4().hex
        self.tip = 10_000_001 + (uuid.uuid4().int % 10_000_000) * 5
        self.block_height = self.tip + 4
        self.username = "qa_" + self.marker
        base58_marker = self.marker.replace("0", "z")
        self.address = "4" + base58_marker + ("a" * 62)
        self.house_address = "8" + base58_marker + ("b" * 62)
        # Production lookup deliberately uses this unique role label. The
        # disposable test database must not contain deployment data.
        self.house_label = "house"
        self._seed_fixture()

    def tearDown(self):
        conn = self.connect()
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM withdrawals WHERE user_id = %s", (self.user_id,))
            cur.execute("DELETE FROM bets WHERE user_id = %s", (self.user_id,))
            cur.execute("DELETE FROM blocks WHERE block_height = %s", (self.block_height,))
            cur.execute("DELETE FROM wallets WHERE id = %s", (self.wallet_id,))
            cur.execute("DELETE FROM users WHERE id = %s", (self.user_id,))
            cur.execute("DELETE FROM admin_wallets WHERE id = %s", (self.admin_wallet_id,))
            conn.commit()
        finally:
            cur.close()
            conn.close()

    def _seed_fixture(self):
        conn = self.connect()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO admin_wallets (wallet_address, balance, label) VALUES (%s,%s,%s)",
                (self.house_address, self.INITIAL_BALANCE, self.house_label),
            )
            self.admin_wallet_id = cur.lastrowid
            cur.execute(
                "INSERT INTO users (username, password_hash, is_admin, admin_wallet_id) "
                "VALUES (%s,'integration-test-only',0,%s)",
                (self.username, self.admin_wallet_id),
            )
            self.user_id = cur.lastrowid
            cur.execute(
                "INSERT INTO wallets (user_id, address, balance, virtual_balance) VALUES (%s,%s,%s,%s)",
                (self.user_id, self.address, self.INITIAL_BALANCE, self.INITIAL_BALANCE),
            )
            self.wallet_id = cur.lastrowid
            cur.execute("UPDATE users SET wallet_id=%s WHERE id=%s", (self.wallet_id, self.user_id))
            conn.commit()
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def _validating_wallet_call(method, params=None):
        if method == "validate_address":
            return {"valid": True}
        raise AssertionError("unexpected wallet call during reservation: " + method)

    def _row(self, sql, params=()):
        conn = self.connect()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params)
            return cur.fetchone()
        finally:
            cur.close()
            conn.close()

    def test_concurrent_bets_and_withdrawals_cannot_overspend(self):
        barrier = threading.Barrier(10)

        def bet_attempt(index):
            barrier.wait()
            try:
                return ("bet", betting.place_bet(
                    self.user_id, str(self.ATTEMPT_AMOUNT), "odd_even",
                    "odd" if index % 2 else "even",
                ))
            except ValueError:
                return ("rejected", None)

        def withdrawal_attempt(index):
            barrier.wait()
            try:
                row = withdrawals.reserve_withdrawal(
                    self.user_id, "user", self.address,
                    str(self.ATTEMPT_AMOUNT), str(uuid.uuid4()),
                )
                return ("withdrawal", row["id"])
            except ValueError:
                return ("rejected", None)

        tasks = [lambda i=i: bet_attempt(i) for i in range(5)]
        tasks += [lambda i=i: withdrawal_attempt(i) for i in range(5)]
        with patch.object(betting, "get_chain_tip", return_value=self.tip), patch.object(
            withdrawals.wallet_rpc, "call", side_effect=self._validating_wallet_call
        ):
            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(lambda task: task(), tasks))

        accepted = sum(kind in ("bet", "withdrawal") for kind, _ in results)
        self.assertLessEqual(accepted * self.ATTEMPT_AMOUNT, self.INITIAL_BALANCE)
        wallet = self._row("SELECT virtual_balance FROM wallets WHERE id=%s", (self.wallet_id,))
        self.assertEqual(
            Decimal(wallet["virtual_balance"]),
            self.INITIAL_BALANCE - accepted * self.ATTEMPT_AMOUNT,
        )

    def test_duplicate_request_id_reserves_funds_once(self):
        request_id = str(uuid.uuid4())
        barrier = threading.Barrier(2)

        def reserve():
            barrier.wait()
            return withdrawals.reserve_withdrawal(
                self.user_id, "user", self.address, "0.4", request_id
            )

        with patch.object(withdrawals.wallet_rpc, "call", side_effect=self._validating_wallet_call):
            with ThreadPoolExecutor(max_workers=2) as executor:
                rows = list(executor.map(lambda _: reserve(), range(2)))

        self.assertEqual(rows[0]["id"], rows[1]["id"])
        stored = self._row(
            "SELECT COUNT(*) count, COALESCE(SUM(amount),0) amount FROM withdrawals "
            "WHERE user_id=%s AND request_id=%s",
            (self.user_id, request_id),
        )
        self.assertEqual(stored["count"], 1)
        self.assertEqual(Decimal(stored["amount"]), Decimal("0.400000000000"))
        wallet = self._row("SELECT virtual_balance FROM wallets WHERE id=%s", (self.wallet_id,))
        self.assertEqual(Decimal(wallet["virtual_balance"]), Decimal("0.600000000000"))

    def test_duplicate_request_id_rejects_changed_details(self):
        request_id = str(uuid.uuid4())
        with patch.object(withdrawals.wallet_rpc, "call", side_effect=self._validating_wallet_call):
            withdrawals.reserve_withdrawal(
                self.user_id, "user", self.address, "0.4", request_id
            )
            with self.assertRaises(ValueError):
                withdrawals.reserve_withdrawal(
                    self.user_id, "user", self.address, "0.5", request_id
                )

        wallet = self._row("SELECT virtual_balance FROM wallets WHERE id=%s", (self.wallet_id,))
        self.assertEqual(Decimal(wallet["virtual_balance"]), Decimal("0.600000000000"))

    def _reserved_withdrawal(self, amount="0.25"):
        with patch.object(withdrawals.wallet_rpc, "call", side_effect=self._validating_wallet_call):
            return withdrawals.reserve_withdrawal(
                self.user_id, "user", self.address, amount, str(uuid.uuid4())
            )

    def test_prepared_transaction_is_durable_before_relay(self):
        withdrawal = self._reserved_withdrawal()
        observed = {}
        prepared_params = {}
        relayed_params = {}

        def rpc(method, params=None):
            if method == "transfer":
                prepared_params.update(params)
                return {
                    "amount": 250000000000,
                    "fee": 10000000,
                    "tx_hash": "1" * 64,
                    "tx_metadata": "deadbeef",
                }
            if method == "relay_tx":
                relayed_params.update(params)
                row = self._row("SELECT * FROM withdrawals WHERE id=%s", (withdrawal["id"],))
                observed.update(row)
                return {"tx_hash": "1" * 64}
            raise AssertionError("unexpected wallet method: " + method)

        with patch.object(withdrawals.wallet_rpc, "call", side_effect=rpc):
            result = withdrawals.process_withdrawal(withdrawal["id"])

        self.assertEqual(observed["state"], "prepared")
        self.assertEqual(observed["tx_hash"], "1" * 64)
        self.assertEqual(observed["tx_metadata"], "deadbeef")
        self.assertIs(prepared_params["do_not_relay"], True)
        self.assertIs(prepared_params["get_tx_metadata"], True)
        self.assertEqual(relayed_params["hex"], "deadbeef")
        self.assertEqual(result["state"], "submitted")

    def test_admin_withdrawal_reserves_house_funds_and_records_submission(self):
        conn = self.connect()
        cur = conn.cursor()
        try:
            cur.execute("UPDATE users SET is_admin=1 WHERE id=%s", (self.user_id,))
            conn.commit()
        finally:
            cur.close()
            conn.close()

        with patch.object(withdrawals.wallet_rpc, "call", side_effect=self._validating_wallet_call):
            withdrawal = withdrawals.reserve_withdrawal(
                self.user_id, "admin", self.address, "0.2", str(uuid.uuid4())
            )

        seen = {}

        def rpc(method, params=None):
            if method == "transfer":
                seen["transfer"] = params
                return {
                    "amount": 200000000000,
                    "fee": 10000000,
                    "tx_hash": "4" * 64,
                    "tx_metadata": "facefeed",
                }
            if method == "relay_tx":
                seen["relay"] = params
                return {"tx_hash": "4" * 64}
            raise AssertionError("unexpected wallet method: " + method)

        with patch.object(withdrawals.wallet_rpc, "call", side_effect=rpc):
            result = withdrawals.process_withdrawal(withdrawal["id"])

        self.assertEqual(result["state"], "submitted")
        self.assertIsNone(withdrawal["wallet_id"])
        self.assertIs(seen["transfer"]["do_not_relay"], True)
        self.assertEqual(seen["relay"]["hex"], "facefeed")
        house = self._row("SELECT balance FROM admin_wallets WHERE id=%s", (self.admin_wallet_id,))
        self.assertEqual(Decimal(house["balance"]), Decimal("0.799990000000"))
        ledger = self._row(
            "SELECT COUNT(*) count FROM admin_transactions WHERE tx_hash=%s",
            ("4" * 64,),
        )
        self.assertEqual(ledger["count"], 1)

    def test_rejected_prepare_releases_reservation_exactly_once(self):
        withdrawal = self._reserved_withdrawal()

        with patch.object(
            withdrawals.wallet_rpc,
            "call",
            side_effect=withdrawals.wallet_rpc.WalletRPCError("rejected"),
        ):
            first = withdrawals.process_withdrawal(withdrawal["id"])
            second = withdrawals.process_withdrawal(withdrawal["id"])

        self.assertEqual(first["state"], "failed")
        self.assertEqual(second["state"], "failed")
        wallet = self._row("SELECT virtual_balance FROM wallets WHERE id=%s", (self.wallet_id,))
        self.assertEqual(Decimal(wallet["virtual_balance"]), self.INITIAL_BALANCE)

    def test_ambiguous_prepare_failure_remains_reserved_and_never_relays(self):
        withdrawal = self._reserved_withdrawal()
        calls = []

        def rpc(method, params=None):
            calls.append(method)
            if method == "transfer":
                raise TimeoutError("unknown prepare outcome")
            raise AssertionError("an ambiguous prepare must not be relayed")

        with patch.object(withdrawals.wallet_rpc, "call", side_effect=rpc):
            result = withdrawals.process_withdrawal(withdrawal["id"])

        self.assertEqual(result["state"], "reserved")
        self.assertEqual(calls, ["transfer"])
        wallet = self._row("SELECT virtual_balance FROM wallets WHERE id=%s", (self.wallet_id,))
        self.assertEqual(Decimal(wallet["virtual_balance"]), Decimal("0.750000000000"))

    def test_relay_timeout_reconciles_without_second_transfer(self):
        withdrawal = self._reserved_withdrawal()
        calls = {"transfer": 0, "relay": 0, "lookup": 0}

        def rpc(method, params=None):
            if method == "transfer":
                calls["transfer"] += 1
                return {
                    "amount": 250000000000,
                    "fee": 10000000,
                    "tx_hash": "2" * 64,
                    "tx_metadata": "cafebabe",
                }
            if method == "relay_tx":
                calls["relay"] += 1
                if calls["relay"] == 1:
                    raise TimeoutError("simulated response loss after broadcast")
                return {"tx_hash": "2" * 64}
            if method == "get_transfer_by_txid":
                calls["lookup"] += 1
                return {"transfer": {"txid": "2" * 64, "type": "pending", "amount": 250000000000}}
            raise AssertionError("unexpected wallet method: " + method)

        with patch.object(withdrawals.wallet_rpc, "call", side_effect=rpc):
            first_result = withdrawals.process_withdrawal(withdrawal["id"])
            self.assertEqual(first_result["state"], "prepared")
            result = withdrawals.process_withdrawal(withdrawal["id"])

        self.assertEqual(calls["transfer"], 1)
        self.assertGreaterEqual(calls["lookup"], 1)
        self.assertEqual(result["state"], "submitted")

    def test_missing_hash_re_relays_the_same_saved_metadata(self):
        withdrawal = self._reserved_withdrawal()
        calls = {"transfer": 0, "relay_metadata": [], "lookup": 0}

        def rpc(method, params=None):
            if method == "transfer":
                calls["transfer"] += 1
                return {
                    "amount": 250000000000,
                    "fee": 10000000,
                    "tx_hash": "5" * 64,
                    "tx_metadata": "0123abcd",
                }
            if method == "relay_tx":
                calls["relay_metadata"].append(params["hex"])
                if len(calls["relay_metadata"]) == 1:
                    raise TimeoutError("simulated lost relay response")
                return {"tx_hash": "5" * 64}
            if method == "get_transfer_by_txid":
                calls["lookup"] += 1
                raise withdrawals.wallet_rpc.WalletRPCError("not found")
            raise AssertionError("unexpected wallet method: " + method)

        with patch.object(withdrawals.wallet_rpc, "call", side_effect=rpc):
            first = withdrawals.process_withdrawal(withdrawal["id"])
            result = withdrawals.process_withdrawal(withdrawal["id"])

        self.assertEqual(first["state"], "prepared")
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(calls["transfer"], 1)
        self.assertEqual(calls["lookup"], 1)
        self.assertEqual(calls["relay_metadata"], ["0123abcd", "0123abcd"])

    def test_commit_failure_after_broadcast_recovers_without_new_transfer(self):
        withdrawal = self._reserved_withdrawal()
        calls = {"transfer": 0, "relay": 0, "lookup": 0}

        def rpc(method, params=None):
            if method == "transfer":
                calls["transfer"] += 1
                return {
                    "amount": 250000000000,
                    "fee": 10000000,
                    "tx_hash": "3" * 64,
                    "tx_metadata": "abcdef12",
                }
            if method == "relay_tx":
                calls["relay"] += 1
                return {"tx_hash": "3" * 64}
            if method == "get_transfer_by_txid":
                calls["lookup"] += 1
                return {"transfer": {"txid": "3" * 64, "type": "pending", "amount": 250000000000}}
            raise AssertionError("unexpected wallet method: " + method)

        real_connection = self.connect()

        class FailSecondCommit:
            def __init__(self, connection):
                self.connection = connection
                self.commits = 0

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                self.commits += 1
                if self.commits == 2:
                    raise RuntimeError("simulated commit loss after broadcast")
                return self.connection.commit()

        with patch.object(withdrawals.wallet_rpc, "call", side_effect=rpc), patch.object(
            withdrawals, "get_db_connection", return_value=FailSecondCommit(real_connection)
        ):
            first = withdrawals.process_withdrawal(withdrawal["id"])

        self.assertEqual(first["state"], "prepared")
        with patch.object(withdrawals.wallet_rpc, "call", side_effect=rpc):
            recovered = withdrawals.process_withdrawal(withdrawal["id"])

        self.assertEqual(recovered["state"], "submitted")
        self.assertEqual(calls["transfer"], 1)
        self.assertEqual(calls["relay"], 1)
        self.assertGreaterEqual(calls["lookup"], 1)


if __name__ == "__main__":
    unittest.main()
