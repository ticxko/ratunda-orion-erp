"""Layer 3 dedup: same account, same date, same amount, same direction.

Port of bellatrix-python tests/accounting/test_same_account_dedup.py,
converted to stdlib unittest. The two named cases are the production
duplicates this layer was written for (reversed on 2026-07-25); the rest guard
against over-flagging, which would silently drop real transactions.
"""

import os
import sys
import unittest
from collections import Counter
from decimal import Decimal

# `python3 -m unittest discover …/tests` (3.11+) treats the tests dir itself as
# top-level, so make the app repo root importable for `orion.accounting.*`.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
	sys.path.insert(0, _REPO_ROOT)

from orion.accounting.same_account_dedup import (
	BatchTxn,
	dup_key,
	find_same_account_duplicates,
)


def _tx(idx: int, date: str, amount, direction: str, already: bool = False) -> BatchTxn:
	return BatchTxn(
		idx=idx,
		date_key=date,
		amount=amount,
		direction=direction,
		already_duplicate=already,
	)


class TestFindSameAccountDuplicates(unittest.TestCase):
	def test_flags_kopra_7100000_reparsed_with_a_different_reference(self):
		"""Voucher token as reference on one export, real bank ref on the other."""
		ledger = Counter({dup_key("2026-06-21", 7100000, "OUT"): 1})
		batch = [_tx(0, "2026-06-21", 7100000, "OUT")]

		self.assertEqual(find_same_account_duplicates(batch, ledger), {0})

	def test_flags_jago_2000000_reparsed_without_a_reference(self):
		"""Second export carried no reference at all, so the hash fell back."""
		ledger = Counter({dup_key("2026-06-04", 2000000, "IN"): 1})
		batch = [_tx(0, "2026-06-04", 2000000, "IN")]

		self.assertEqual(find_same_account_duplicates(batch, ledger), {0})

	def test_does_not_flag_repeated_same_day_fees_that_are_all_new(self):
		"""Fifteen genuine Rp 5.000 fees on one day, none previously imported."""
		batch = [_tx(i, "2026-05-08", 5000, "OUT") for i in range(15)]

		self.assertEqual(find_same_account_duplicates(batch, Counter()), set())

	def test_flags_only_the_excess_when_the_ledger_holds_some_already(self):
		"""Ledger has 15 fees, batch carries 16 — exactly one is genuinely new."""
		ledger = Counter({dup_key("2026-05-08", 5000, "OUT"): 15})
		batch = [_tx(i, "2026-05-08", 5000, "OUT") for i in range(16)]

		flagged = find_same_account_duplicates(batch, ledger)
		self.assertEqual(len(flagged), 15)
		self.assertNotIn(15, flagged)  # the last one survives as new

	def test_earlier_layer_matches_consume_their_ledger_slot_first(self):
		"""A hash-matched txn must not let a second copy through on the same key."""
		ledger = Counter({dup_key("2026-06-21", 7100000, "OUT"): 1})
		batch = [
			_tx(0, "2026-06-21", 7100000, "OUT", already=True),
			_tx(1, "2026-06-21", 7100000, "OUT"),
		]

		# The ledger holds one occurrence and Layer 1 already claimed it, so the
		# second line is genuinely new rather than a duplicate.
		self.assertEqual(find_same_account_duplicates(batch, ledger), set())

	def test_direction_distinguishes_a_refund_from_the_payment(self):
		"""Same day, same amount, opposite direction — two real transactions."""
		ledger = Counter({dup_key("2026-06-10", 1500000, "OUT"): 1})
		batch = [_tx(0, "2026-06-10", 1500000, "IN")]

		self.assertEqual(find_same_account_duplicates(batch, ledger), set())

	def test_date_distinguishes_otherwise_identical_transactions(self):
		ledger = Counter({dup_key("2026-06-10", 1500000, "OUT"): 1})
		batch = [_tx(0, "2026-06-11", 1500000, "OUT")]

		self.assertEqual(find_same_account_duplicates(batch, ledger), set())

	def test_amount_forms_compare_equal_across_int_float_and_decimal(self):
		ledger = Counter({dup_key("2026-06-21", Decimal("7100000.00"), "OUT"): 1})
		batch = [_tx(0, "2026-06-21", 7100000.0, "OUT")]

		self.assertEqual(find_same_account_duplicates(batch, ledger), {0})

	def test_fractional_amounts_are_not_conflated(self):
		ledger = Counter({dup_key("2026-06-21", Decimal("638250.50"), "OUT"): 1})
		batch = [_tx(0, "2026-06-21", Decimal("638250.55"), "OUT")]

		self.assertEqual(find_same_account_duplicates(batch, ledger), set())

	def test_already_flagged_transactions_are_never_reported_twice(self):
		ledger = Counter({dup_key("2026-06-04", 2000000, "IN"): 1})
		batch = [_tx(0, "2026-06-04", 2000000, "IN", already=True)]

		self.assertEqual(find_same_account_duplicates(batch, ledger), set())


if __name__ == "__main__":
	unittest.main()
