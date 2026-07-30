"""Port of bellatrix-python tests/accounting/test_loan_events.py, converted
to stdlib unittest.

The codes must match the ones PT POI already issues over email
(PTPOI-CL04-26VII / POI-PB03-26VI-PTPOI) so Orion replaces that process
rather than inventing a parallel numbering scheme.
"""

import unittest
from datetime import datetime
from decimal import Decimal

from orion.accounting.loan_codes import build_doc_code, recompute_outstanding, roman_month


class TestDocumentCodes(unittest.TestCase):
	def test_drawdown_code_matches_real_capital_loan_format(self):
		code = build_doc_code(direction="DRAWDOWN", sequence=4, when=datetime(2026, 7, 7))
		self.assertEqual(code, "PTPOI-CL04-26VII")

	def test_repayment_code_matches_real_payback_format(self):
		code = build_doc_code(direction="REPAYMENT", sequence=3, when=datetime(2026, 6, 23))
		self.assertEqual(code, "POI-PB03-26VI-PTPOI")

	def test_sequence_is_zero_padded_to_two_digits(self):
		code = build_doc_code(direction="DRAWDOWN", sequence=1, when=datetime(2026, 6, 28))
		self.assertEqual(code, "PTPOI-CL01-26VI")

	def test_sequence_beyond_ninety_nine_still_renders(self):
		code = build_doc_code(direction="DRAWDOWN", sequence=100, when=datetime(2026, 1, 5))
		self.assertEqual(code, "PTPOI-CL100-26I")

	def test_custom_prefix_is_honoured(self):
		code = build_doc_code(
			direction="DRAWDOWN", sequence=2, when=datetime(2026, 12, 1), prefix="POI-XL"
		)
		self.assertEqual(code, "POI-XL02-26XII")

	def test_roman_month_covers_all_twelve(self):
		months = [roman_month(datetime(2026, m, 1)) for m in range(1, 13)]
		self.assertEqual(
			months,
			["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"],
		)


class TestOutstandingBalance(unittest.TestCase):
	def test_outstanding_is_drawdowns_less_repayments(self):
		events = [
			("DRAWDOWN", Decimal("145000000")),
			("REPAYMENT", Decimal("6000000")),
			("DRAWDOWN", Decimal("13300000")),
		]
		self.assertEqual(recompute_outstanding(events), Decimal("152300000"))

	def test_outstanding_never_goes_negative(self):
		events = [("DRAWDOWN", Decimal("1000")), ("REPAYMENT", Decimal("5000"))]
		self.assertEqual(recompute_outstanding(events), Decimal("0"))

	def test_outstanding_of_empty_facility_is_zero(self):
		self.assertEqual(recompute_outstanding([]), Decimal("0"))

	def test_reconstructs_the_verified_lastiko_position(self):
		"""Drawdowns 266,650,000 less repayments 42,023,332 = 224,626,668.

		Figures verified independently from the BCA and Jago statements.
		"""
		events = [
			("DRAWDOWN", Decimal("266650000")),
			("REPAYMENT", Decimal("42023332")),
		]
		self.assertEqual(recompute_outstanding(events), Decimal("224626668"))


if __name__ == "__main__":
	unittest.main()
