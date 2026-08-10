"""Unit tests for the installment schedule generator (stdlib unittest).

The DBS KTA is the reference case: principal 150,000,000 over 36 months at a
fixed installment of 7,011,666 → principal 4,166,666 / interest 2,845,000 per
month, principal portions summing to exactly 150,000,000.
"""

import unittest
from datetime import date
from decimal import Decimal

from orion.accounting.loan_schedule import add_months, build_schedule


class TestAddMonths(unittest.TestCase):
	def test_simple_add(self):
		self.assertEqual(add_months("2026-02-27", 1), date(2026, 3, 27))

	def test_year_rollover(self):
		self.assertEqual(add_months("2026-11-15", 3), date(2027, 2, 15))

	def test_day_clamped_to_month_end(self):
		# 31 Jan + 1 month has no 31 Feb → clamps to 28 Feb 2026.
		self.assertEqual(add_months("2026-01-31", 1), date(2026, 2, 28))


class TestBuildSchedule(unittest.TestCase):
	def setUp(self):
		self.rows = build_schedule(
			start_date="2026-02-27",
			tenor_months=36,
			monthly_installment=7011666,
			principal=150000000,
		)

	def test_row_count_matches_tenor(self):
		self.assertEqual(len(self.rows), 36)

	def test_regular_row_splits_principal_and_interest(self):
		first = self.rows[0]
		self.assertEqual(first["installment_no"], 1)
		self.assertEqual(first["due_date"], "2026-02-27")
		self.assertEqual(first["due_principal"], Decimal("4166666"))
		self.assertEqual(first["due_interest"], Decimal("2845000"))
		self.assertEqual(first["due_total"], Decimal("7011666"))

	def test_principal_portions_sum_exactly_to_principal(self):
		total_principal = sum((r["due_principal"] for r in self.rows), Decimal("0"))
		self.assertEqual(total_principal, Decimal("150000000"))

	def test_final_row_absorbs_rounding_remainder(self):
		last = self.rows[-1]
		self.assertEqual(last["installment_no"], 36)
		self.assertEqual(last["due_date"], "2029-01-27")
		# 150,000,000 - 4,166,666*35 = 4,166,690 on the final row.
		self.assertEqual(last["due_principal"], Decimal("4166690"))

	def test_due_dates_advance_one_month_each(self):
		self.assertEqual(self.rows[1]["due_date"], "2026-03-27")
		self.assertEqual(self.rows[11]["due_date"], "2027-01-27")

	def test_zero_tenor_returns_empty(self):
		self.assertEqual(build_schedule(
			start_date="2026-02-27", tenor_months=0,
			monthly_installment=1, principal=1), [])

	def test_interest_never_negative(self):
		# Installment smaller than the principal slice → interest floored at 0.
		rows = build_schedule(
			start_date="2026-01-01", tenor_months=2,
			monthly_installment=100, principal=1000)
		self.assertTrue(all(r["due_interest"] >= 0 for r in rows))


if __name__ == "__main__":
	unittest.main()
