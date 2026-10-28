"""Opt-in disposable-MySQL tests for deposit review holds."""

import os
import unittest
import uuid
from decimal import Decimal
from unittest.mock import patch

import mysql.connector

import betting
import withdrawals


ENABLED = os.getenv("CASINO_INTEGRATION_TESTS") == "1"


def connect():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"), port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "casino_admin"), password=os.getenv("DB_PASSWORD", ""),
        database=os.environ["DB_NAME"])


@unittest.skipUnless(ENABLED, "set CASINO_INTEGRATION_TESTS=1 for disposable-MySQL tests")
class DepositHoldIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = os.environ.get("DB_NAME", "")
        if not any(marker in name.lower() for marker in ("test", "qa")):
            raise unittest.SkipTest("DB_NAME must identify a disposable test/QA database")

    def setUp(self):
        self.conn = connect()
        cur = self.conn.cursor()
        marker = uuid.uuid4().hex
        cur.execute("SELECT id FROM admin_wallets WHERE label='house'")
        if cur.fetchone():
            cur.close()
            self.conn.close()
            raise RuntimeError("deposit-hold fixture requires no pre-existing house row")
        cur.execute("INSERT INTO admin_wallets(wallet_address,balance,label) VALUES(%s,10,%s)",
                    ("8" + marker + "a" * 62, "house"))
        self.house_id = cur.lastrowid
        cur.execute("INSERT INTO users(username,password_hash) VALUES(%s,'test')", ("hold_" + marker,))
        self.user_id = cur.lastrowid
        cur.execute("""INSERT INTO wallets(user_id,address,virtual_balance,deposit_hold)
                       VALUES(%s,%s,1,1)""", (self.user_id, "4" + marker + "b" * 62))
        self.wallet_id = cur.lastrowid
        cur.execute("UPDATE users SET wallet_id=%s WHERE id=%s", (self.wallet_id, self.user_id))
        self.conn.commit()
        cur.close()

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT block_id FROM bets WHERE user_id=%s", (self.user_id,))
        block_ids = [row[0] for row in cur.fetchall()]
        cur.execute("DELETE FROM withdrawals WHERE user_id=%s", (self.user_id,))
        cur.execute("DELETE FROM bets WHERE user_id=%s", (self.user_id,))
        for block_id in block_ids:
            cur.execute("DELETE FROM blocks WHERE id=%s AND NOT EXISTS "
                        "(SELECT 1 FROM bets WHERE block_id=%s)", (block_id, block_id))
        cur.execute("UPDATE users SET wallet_id=NULL WHERE id=%s", (self.user_id,))
        cur.execute("DELETE FROM wallets WHERE id=%s", (self.wallet_id,))
        cur.execute("DELETE FROM users WHERE id=%s", (self.user_id,))
        cur.execute("DELETE FROM admin_wallets WHERE id=%s", (self.house_id,))
        self.conn.commit()
        cur.close()
        self.conn.close()

    def test_hold_rejects_new_bet_without_debit(self):
        with patch.object(betting, "get_chain_tip", return_value=101):
            with self.assertRaisesRegex(ValueError, "review"):
                betting.place_bet(self.user_id, "0.1", "digits", "05")
        cur = self.conn.cursor()
        cur.execute("SELECT virtual_balance FROM wallets WHERE id=%s", (self.wallet_id,))
        self.assertEqual(str(cur.fetchone()[0]), "1.000000000000")
        cur.close()

    def test_hold_rejects_new_user_withdrawal_without_reservation(self):
        with patch.object(withdrawals.wallet_rpc, "call", return_value={"valid": True}):
            with self.assertRaisesRegex(ValueError, "review"):
                withdrawals.reserve_withdrawal(
                    self.user_id, "user", "4" + "a" * 94, "0.1", str(uuid.uuid4()))
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM withdrawals WHERE user_id=%s", (self.user_id,))
        self.assertEqual(cur.fetchone()[0], 0)
        cur.close()

    def test_reserved_withdrawal_remains_reserved_and_is_not_prepared(self):
        cur = self.conn.cursor()
        cur.execute("""INSERT INTO withdrawals
                       (request_id,user_id,kind,wallet_id,admin_wallet_id,address,amount,state)
                       VALUES(%s,%s,'user',%s,%s,%s,0.1,'reserved')""",
                    (str(uuid.uuid4()), self.user_id, self.wallet_id, self.house_id, "4" + "a" * 94))
        withdrawal_id = cur.lastrowid
        self.conn.commit()
        cur.close()
        with patch.object(withdrawals.wallet_rpc, "call") as rpc:
            result = withdrawals.process_withdrawal(withdrawal_id)
        self.assertEqual(result["state"], "reserved")
        rpc.assert_not_called()

    def _prepared_withdrawal(self, tx_hash):
        cur = self.conn.cursor()
        cur.execute("""INSERT INTO withdrawals
                       (request_id,user_id,kind,wallet_id,admin_wallet_id,address,amount,
                        fee,state,tx_hash,tx_metadata)
                       VALUES(%s,%s,'user',%s,%s,%s,0.1,0.01,'prepared',%s,'aa')""",
                    (str(uuid.uuid4()), self.user_id, self.wallet_id, self.house_id,
                     "4" + "a" * 94, tx_hash))
        withdrawal_id = cur.lastrowid
        self.conn.commit()
        cur.close()
        return withdrawal_id

    def test_prepared_held_withdrawal_is_not_relayed_when_not_known(self):
        tx_hash = "a" * 64
        withdrawal_id = self._prepared_withdrawal(tx_hash)
        with patch.object(withdrawals.wallet_rpc, "call", return_value={"transfers": []}) as rpc:
            result = withdrawals.process_withdrawal(withdrawal_id)
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(rpc.call_args.args[0], "get_transfer_by_txid")

    def test_prepared_held_broadcast_is_finalized_without_relay(self):
        tx_hash = "b" * 64
        withdrawal_id = self._prepared_withdrawal(tx_hash)
        known = {"transfers": [{"txid": tx_hash, "type": "out", "amount": 100_000_000_000}]}
        with patch.object(withdrawals.wallet_rpc, "call", return_value=known) as rpc:
            result = withdrawals.process_withdrawal(withdrawal_id)
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(rpc.call_args.args[0], "get_transfer_by_txid")
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM transactions WHERE user_id=%s AND tx_hash=%s",
                    (self.user_id, tx_hash))
        self.assertEqual(cur.fetchone()[0], 1)
        cur.execute("SELECT balance FROM admin_wallets WHERE id=%s", (self.house_id,))
        self.assertEqual(cur.fetchone()[0], Decimal("9.890000000000"))
        cur.close()

    def test_any_hold_rejects_new_admin_reservation_without_house_debit(self):
        cur = self.conn.cursor()
        cur.execute("UPDATE users SET is_admin=1 WHERE id=%s", (self.user_id,))
        self.conn.commit()
        cur.close()
        with patch.object(withdrawals.wallet_rpc, "call", return_value={"valid": True}):
            with self.assertRaisesRegex(ValueError, "review"):
                withdrawals.reserve_withdrawal(
                    self.user_id, "admin", "4" + "a" * 94, "0.1", str(uuid.uuid4()))
        cur = self.conn.cursor()
        cur.execute("SELECT balance FROM admin_wallets WHERE id=%s", (self.house_id,))
        self.assertEqual(cur.fetchone()[0], Decimal("10.000000000000"))
        cur.execute("SELECT COUNT(*) FROM withdrawals WHERE user_id=%s", (self.user_id,))
        self.assertEqual(cur.fetchone()[0], 0)
        cur.close()


if __name__ == "__main__":
    unittest.main()
