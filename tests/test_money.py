import unittest
from decimal import Decimal

from money import parse_amount, atomic_units


class AmountTests(unittest.TestCase):
    def test_rejects_invalid_nonfinite_negative_and_subatomic(self):
        for value in (None, '', 'NaN', 'sNaN', 'Infinity', '-Infinity', '-1', '0',
                      '0.0000000000001', '1e1000', '100000000', 0.1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_amount(value)

    def test_exact_atomic_conversion_and_minimum(self):
        self.assertEqual(atomic_units(parse_amount('0.224399999999')), 224399999999)
        self.assertEqual(atomic_units(parse_amount('0.000000000001')), 1)
        with self.assertRaises(ValueError):
            parse_amount('0.049999999999', minimum=Decimal('0.05'))
