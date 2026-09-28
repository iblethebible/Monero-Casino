import os
import unittest
from unittest.mock import patch

import deposits


ADDRESS = "4" + "a" * 94
ADDRESSES = {1: ADDRESS}


def transfer(txid="1" * 64, **overrides):
    value = {
        "txid": txid,
        "address": ADDRESS,
        "amount": 1_000_000_000_000,
        "height": 111,
        "confirmations": 10,
        "locked": False,
        "double_spend_seen": False,
        "unlock_time": 0,
        "subaddr_index": {"major": 0, "minor": 1},
    }
    value.update(overrides)
    return value


class NormalizeTransfersTests(unittest.TestCase):
    def test_duplicate_recipient_output_is_rejected(self):
        item = transfer()
        with self.assertRaises(deposits.SnapshotError):
            deposits.normalize_transfers([item, dict(item)], ADDRESSES)

    def test_aggregate_component_amount_is_counted_once(self):
        normalized = deposits.normalize_transfers(
            [transfer(amount=10, amounts=[4, 6])], ADDRESSES
        )
        self.assertEqual(str(normalized[0]["amount"]), "1E-11")

    def test_inconsistent_component_amounts_are_rejected(self):
        with self.assertRaises(deposits.SnapshotError):
            deposits.normalize_transfers(
                [transfer(amount=10, amounts=[4, 7])], ADDRESSES
            )

    def test_address_must_match_wallet_index_mapping(self):
        with self.assertRaises(deposits.SnapshotError):
            deposits.normalize_transfers(
                [transfer(address="8" + "b" * 94)], ADDRESSES
            )

    def test_optional_component_amounts_may_be_omitted(self):
        normalized = deposits.normalize_transfers([transfer()], ADDRESSES)
        self.assertEqual(len(normalized), 1)

    def test_present_invalid_component_amounts_are_rejected(self):
        invalid_values = (None, [], [1, "bad"], [0, 1_000_000_000_000])
        for amounts in invalid_values:
            with self.subTest(amounts=amounts):
                with self.assertRaises(deposits.SnapshotError):
                    deposits.normalize_transfers(
                        [transfer(amounts=amounts)], ADDRESSES
                    )


class SnapshotBoundaryTests(unittest.TestCase):
    @staticmethod
    def _header(height):
        return {"height": int(height), "hash": f"{int(height):064x}"}

    def _snapshot(self, first, second=None):
        return self._snapshot_results(
            {"in": first}, {"in": first if second is None else second}
        )

    def _snapshot_results(self, first_result, second_result):
        snapshots = iter((first_result, second_result))

        def rpc(method, params=None):
            if method == "get_height":
                return {"height": 121}
            if method == "get_address":
                return {"addresses": [{"address": ADDRESS, "address_index": 1}]}
            if method == "get_transfers":
                return next(snapshots)
            raise AssertionError("unexpected wallet method: " + method)

        with patch.object(deposits.wallet_rpc, "call", side_effect=rpc), patch.object(
            deposits, "get_chain_tip", return_value=120
        ), patch.object(deposits, "get_block_header", side_effect=self._header), patch.dict(
            os.environ, {"MONERO_DEPOSIT_MIN_CONFIRMATIONS": "10"}, clear=False
        ):
            return deposits._snapshot()

    def test_omitted_in_field_is_accepted_as_empty_snapshot(self):
        _, values, _ = self._snapshot_results({}, {})
        self.assertEqual(values, [])

    def test_present_non_list_in_field_is_rejected(self):
        with self.assertRaises(deposits.SnapshotError):
            self._snapshot_results({"in": {}}, {"in": {}})

    def test_changing_wallet_snapshot_is_rejected(self):
        with self.assertRaisesRegex(deposits.SnapshotError, "changed during scan"):
            self._snapshot([transfer()], [transfer(amount=2_000_000_000_000)])

    def test_mature_unlocked_canonical_transfer_is_eligible(self):
        _, values, _ = self._snapshot([transfer()])
        self.assertIs(values[0]["eligible"], True)

    def test_immature_transfer_is_not_eligible(self):
        _, values, _ = self._snapshot([
            transfer(height=112, confirmations=9)
        ])
        self.assertIs(values[0]["eligible"], False)

    def test_double_spend_marker_is_not_eligible(self):
        _, values, _ = self._snapshot([
            transfer(double_spend_seen=True)
        ])
        self.assertIs(values[0]["eligible"], False)

    def test_nonzero_unlock_time_is_not_eligible(self):
        _, values, _ = self._snapshot([transfer(unlock_time=121)])
        self.assertIs(values[0]["eligible"], False)

    def test_locked_transfer_is_not_eligible(self):
        _, values, _ = self._snapshot([transfer(locked=True)])
        self.assertIs(values[0]["eligible"], False)


if __name__ == "__main__":
    unittest.main()
