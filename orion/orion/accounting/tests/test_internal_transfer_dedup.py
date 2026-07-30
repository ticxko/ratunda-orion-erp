"""Cross-account internal-transfer dedup (Layer 1, symmetric XFER hash).

Port of bellatrix-python tests/accounting/test_internal_transfer_dedup.py,
converted to stdlib unittest.

A transfer between two operational accounts appears on BOTH statements. The
reference differs per side and each side names the OTHER account, so the old
per-account hash and the ref-based Layer 2 both miss it — the transfer got
booked twice (see the 2026-07-25 double-import audit, ~Rp 227jt). The symmetric
hash makes both sides collide on the txn_hash UNIQUE constraint.
"""

import os
import sys
import unittest

# `python3 -m unittest discover …/tests` (3.11+) treats the tests dir itself as
# top-level, so make the app repo root importable for `orion.accounting.*`.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
	sys.path.insert(0, _REPO_ROOT)

from orion.accounting.account_mapper import (
	counterparty_operational_account,
	internal_transfer_hash,
	is_operational_account,
)

KOPRA = "1660007677776"   # Mandiri Kopra (PT POI)
JAGO = "103597714740"     # Bank Jago pocket (PT POI)
AMITA = "1660007212202"   # Mandiri Livin (Amita)
OUTSIDE = "9990001112223"  # not an operational account


class TestCounterpartyDetection(unittest.TestCase):
	def test_detects_counterparty_by_account_number_in_remark(self):
		# Importing Kopra's statement; remark names the Jago pocket → internal xfer.
		self.assertEqual(
			counterparty_operational_account(KOPRA, f"Transfer ke Jago {JAGO} PTPOI"),
			JAGO,
		)

	def test_no_counterparty_when_remark_has_no_operational_number(self):
		# Bank-name-only remark (the hard case) — best-effort layer returns None.
		self.assertIsNone(
			counterparty_operational_account(KOPRA, "Outgoing Transfer Payback PTPOI")
		)

	def test_no_counterparty_when_importing_non_operational_statement(self):
		self.assertIsNone(counterparty_operational_account(OUTSIDE, f"Transfer {KOPRA}"))

	def test_transfer_to_external_account_is_not_internal(self):
		self.assertIsNone(
			counterparty_operational_account(KOPRA, f"Transfer ke vendor {OUTSIDE}")
		)

	def test_is_operational_account(self):
		self.assertTrue(is_operational_account(KOPRA))
		self.assertFalse(is_operational_account(OUTSIDE))


class TestSymmetricHash(unittest.TestCase):
	def test_both_directions_of_one_transfer_hash_identically(self):
		# Kopra statement sees money out to Jago; Jago statement sees money in from
		# Kopra. Same date + amount → identical hash regardless of which side.
		h_from_kopra = internal_transfer_hash(KOPRA, JAGO, "2026-05-04", 25_300_000)
		h_from_jago = internal_transfer_hash(JAGO, KOPRA, "2026-05-04", 25_300_000)
		self.assertEqual(h_from_kopra, h_from_jago)

	def test_hash_is_stable_and_readable(self):
		self.assertEqual(
			internal_transfer_hash(KOPRA, JAGO, "2026-05-04T14:00", 25_300_000),
			f"XFER|{JAGO}|{KOPRA}|2026-05-04|25300000.00",
		)

	def test_different_account_pair_hashes_differently(self):
		self.assertNotEqual(
			internal_transfer_hash(KOPRA, JAGO, "2026-05-04", 1_000_000),
			internal_transfer_hash(KOPRA, AMITA, "2026-05-04", 1_000_000),
		)

	def test_different_amount_hashes_differently(self):
		a = internal_transfer_hash(KOPRA, JAGO, "2026-05-04", 1_000_000)
		b = internal_transfer_hash(KOPRA, JAGO, "2026-05-04", 2_000_000)
		self.assertNotEqual(a, b)

	def test_different_date_hashes_differently(self):
		a = internal_transfer_hash(KOPRA, JAGO, "2026-05-04", 1_000_000)
		b = internal_transfer_hash(KOPRA, JAGO, "2026-05-05", 1_000_000)
		self.assertNotEqual(a, b)


if __name__ == "__main__":
	unittest.main()
