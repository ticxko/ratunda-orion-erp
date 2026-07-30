"""Port of bellatrix-python tests/accounting/test_txn_hash.py (itself a port
of Node's src/__tests__/txn-hash.test.ts), converted to stdlib unittest.

Hashes MUST be byte-identical to the Node implementation so already-imported
BANK_IMPORT entries don't get duplicated after cutover.
"""

import os
import sys
import unittest

# `python3 -m unittest discover …/tests` (3.11+) treats the tests dir itself as
# top-level, so make the app repo root importable for `orion.accounting.*`.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
	sys.path.insert(0, _REPO_ROOT)

from orion.accounting.txn_hash import compute_txn_hash, has_real_ref


class TestComputeTxnHash(unittest.TestCase):
	def test_uses_reference_based_hash_when_reference_is_present(self):
		h = compute_txn_hash(
			"1660007677776", "2026-01-15", "BIFA123456", 500000, "Transfer ke John"
		)
		self.assertEqual(h, "1660007677776|ref:BIFA123456")

	def test_falls_back_to_description_hash_when_reference_is_dash(self):
		h = compute_txn_hash(
			"1660007677776", "2026-01-15", "-", 500000, "Transfer ke John Doe"
		)
		self.assertEqual(h, "1660007677776|2026-01-15|500000|transfer ke john doe")

	def test_falls_back_to_description_hash_when_reference_is_empty(self):
		h = compute_txn_hash("1660007677776", "2026-01-15", "", 500000, "Transfer")
		self.assertEqual(h, "1660007677776|2026-01-15|500000|transfer")

	def test_normalises_description_strips_punctuation_lowercases_truncates(self):
		long_desc = "A" * 100
		h = compute_txn_hash("ACC", "2026-01-01", "-", 100, long_desc)
		self.assertIn("|" + ("a" * 50), h)

	def test_produces_identical_hashes_for_same_inputs(self):
		a = compute_txn_hash("ACC", "2026-01-01", "-", 100, "Test")
		b = compute_txn_hash("ACC", "2026-01-01", "-", 100, "Test")
		self.assertEqual(a, b)

	def test_produces_different_hashes_for_different_amounts(self):
		a = compute_txn_hash("ACC", "2026-01-01", "-", 100, "Test")
		b = compute_txn_hash("ACC", "2026-01-01", "-", 200, "Test")
		self.assertNotEqual(a, b)

	def test_produces_different_hashes_for_different_dates(self):
		a = compute_txn_hash("ACC", "2026-01-01", "-", 100, "Test")
		b = compute_txn_hash("ACC", "2026-01-02", "-", 100, "Test")
		self.assertNotEqual(a, b)

	def test_produces_different_hashes_for_different_accounts(self):
		a = compute_txn_hash("ACC1", "2026-01-01", "-", 100, "Test")
		b = compute_txn_hash("ACC2", "2026-01-01", "-", 100, "Test")
		self.assertNotEqual(a, b)


class TestHasRealRef(unittest.TestCase):
	def test_has_real_ref_true_for_real_reference(self):
		self.assertIs(has_real_ref("BIFA123456"), True)

	def test_has_real_ref_false_for_dash(self):
		self.assertIs(has_real_ref("-"), False)

	def test_has_real_ref_false_for_empty_string(self):
		self.assertIs(has_real_ref(""), False)

	def test_has_real_ref_false_for_none(self):
		self.assertIs(has_real_ref(None), False)

	def test_has_real_ref_false_for_whitespace_only(self):
		self.assertIs(has_real_ref("   "), False)

	def test_has_real_ref_false_for_dash_with_spaces(self):
		self.assertIs(has_real_ref(" - "), False)


if __name__ == "__main__":
	unittest.main()
