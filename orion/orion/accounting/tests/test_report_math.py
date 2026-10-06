"""Tests for the pure report arithmetic behind the accounting Excel export.

`python3 -m unittest discover .../tests` (3.11+) treats the tests dir itself as
top-level, so make the app repo root importable for `orion.accounting.*`.
"""

import os
import sys
import unittest
from datetime import date
from itertools import pairwise

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
	sys.path.insert(0, _REPO_ROOT)

from orion.accounting.report_math import (  # noqa: E402,RUF100
	SECTION_INVESTASI,
	SECTION_OPERASI,
	SECTION_ORDER,
	SECTION_OTHER,
	SECTION_PENDANAAN,
	account_tree,
	aging_bucket,
	cash_flow,
	cash_section,
	flatten_tree,
	fold_monthly,
	month_keys,
)


class MonthKeysTest(unittest.TestCase):
	def test_single_month(self):
		self.assertEqual(month_keys(date(2026, 3, 1), date(2026, 3, 31)), [202603])

	def test_range_within_year(self):
		self.assertEqual(
			month_keys(date(2026, 1, 1), date(2026, 4, 30)),
			[202601, 202602, 202603, 202604],
		)

	def test_year_rollover(self):
		self.assertEqual(
			month_keys(date(2025, 11, 1), date(2026, 2, 28)),
			[202511, 202512, 202601, 202602],
		)

	def test_full_live_range(self):
		# Jan 2025 - Sep 2026, the whole of the live data set.
		keys = month_keys(date(2025, 1, 1), date(2026, 9, 30))
		self.assertEqual(len(keys), 21)
		self.assertEqual(keys[0], 202501)
		self.assertEqual(keys[-1], 202609)


class FoldMonthlyTest(unittest.TestCase):
	def test_flow_mode_leaves_gaps_empty(self):
		deltas = {202601: {"A": (100.0, 0.0)}, 202603: {"A": (50.0, 0.0)}}
		months = [202601, 202602, 202603]
		out = fold_monthly(deltas, months, cumulative=False)
		self.assertEqual(out[202601], {"A": (100.0, 0.0)})
		self.assertEqual(out[202602], {})  # no activity, no row
		self.assertEqual(out[202603], {"A": (50.0, 0.0)})

	def test_flow_mode_ignores_out_of_range_months(self):
		deltas = {202512: {"A": (999.0, 0.0)}, 202601: {"A": (10.0, 0.0)}}
		out = fold_monthly(deltas, [202601], cumulative=False)
		self.assertEqual(out, {202601: {"A": (10.0, 0.0)}})

	def test_cumulative_carries_gap_months_forward(self):
		"""The regression this guards: defaulting the CUMULATIVE rather than the
		per-month delta would zero out February."""
		deltas = {202601: {"A": (100.0, 0.0)}, 202603: {"A": (50.0, 0.0)}}
		months = [202601, 202602, 202603]
		out = fold_monthly(deltas, months, cumulative=True)
		self.assertEqual(out[202601]["A"], (100.0, 0.0))
		self.assertEqual(out[202602]["A"], (100.0, 0.0))  # carried, not zeroed
		self.assertEqual(out[202603]["A"], (150.0, 0.0))

	def test_cumulative_folds_pre_range_seed(self):
		deltas = {
			202411: {"A": (70.0, 0.0)},
			202512: {"A": (30.0, 10.0)},
			202601: {"A": (5.0, 0.0)},
		}
		out = fold_monthly(deltas, [202601, 202602], cumulative=True)
		# seed = 70+30 debit, 10 credit; then Jan adds 5 debit
		self.assertEqual(out[202601]["A"], (105.0, 10.0))
		self.assertEqual(out[202602]["A"], (105.0, 10.0))

	def test_cumulative_snapshots_are_independent(self):
		deltas = {202601: {"A": (10.0, 0.0)}, 202602: {"A": (10.0, 0.0)}}
		out = fold_monthly(deltas, [202601, 202602], cumulative=True)
		# January must not have been mutated by February's fold.
		self.assertEqual(out[202601]["A"], (10.0, 0.0))
		self.assertEqual(out[202602]["A"], (20.0, 0.0))

	def test_account_silent_in_period_still_carries(self):
		deltas = {202412: {"A": (80.0, 0.0)}}
		out = fold_monthly(deltas, [202601, 202602], cumulative=True)
		self.assertEqual(out[202601]["A"], (80.0, 0.0))
		self.assertEqual(out[202602]["A"], (80.0, 0.0))


def _leaf(ident, code, name, balance, parent=None):
	return {
		"id": ident, "code": code, "name": name, "balance": balance,
		"parentId": parent, "isHeader": False, "type": "EXPENSE",
	}


def _header(ident, code, name, balance=0.0, parent=None, type_="EXPENSE"):
	return {
		"id": ident, "code": code, "name": name, "balance": balance,
		"parentId": parent, "isHeader": True, "type": type_,
	}


class AccountTreeTest(unittest.TestCase):
	def test_rollup_is_additive_for_non_header_parent(self):
		"""6-1600 with child 6-1602 keeps its own activity plus the child's."""
		leaves = [
			_leaf("a", "6-1600", "Beban Operasional", 100.0),
			_leaf("b", "6-1602", "Beban Listrik", 40.0, parent="a"),
		]
		tree = account_tree(leaves, [], ["EXPENSE"])
		self.assertEqual(len(tree), 1)
		self.assertEqual(tree[0]["code"], "6-1600")
		self.assertEqual(tree[0]["amount"], 140.0)
		self.assertTrue(tree[0]["branch"])
		self.assertEqual(tree[0]["children"][0]["amount"], 40.0)

	def test_synthetic_root_is_promoted_away(self):
		headers = [_header("root", "6-0000", "BEBAN")]
		leaves = [
			_leaf("x", "6-1100", "Gaji", 10.0, parent="root"),
			_leaf("y", "6-1200", "Sewa", 20.0, parent="root"),
		]
		tree = account_tree(leaves, headers, ["EXPENSE"])
		codes = [n["code"] for n in tree]
		self.assertEqual(codes, ["6-1100", "6-1200"])  # 6-0000 gone

	def test_header_rollup_reaches_header_rows(self):
		"""The reason this port exists: a header's own balance is ~0, so without
		rollup every header row in the spreadsheet would read 0."""
		headers = [
			_header("root", "6-0000", "BEBAN"),
			_header("grp", "6-1000", "Beban Usaha", 0.0, parent="root"),
		]
		leaves = [
			_leaf("x", "6-1100", "Gaji", 10.0, parent="grp"),
			_leaf("y", "6-1200", "Sewa", 20.0, parent="grp"),
		]
		tree = account_tree(leaves, headers, ["EXPENSE"])
		self.assertEqual(len(tree), 1)
		self.assertEqual(tree[0]["code"], "6-1000")
		self.assertEqual(tree[0]["amount"], 30.0)

	def test_header_of_other_type_orphans_children(self):
		"""Reproduced quirk, not a bug to fix: the header is filtered out by
		type, so its children become roots. Excel must match the screen."""
		headers = [_header("grp", "4-1000", "Pendapatan", 0.0, type_="REVENUE")]
		leaves = [_leaf("x", "6-1100", "Gaji", 10.0, parent="grp")]
		tree = account_tree(leaves, headers, ["EXPENSE"])
		self.assertEqual([n["code"] for n in tree], ["6-1100"])

	def test_sorted_by_code_recursively(self):
		leaves = [
			_leaf("a", "6-1600", "Ops", 0.0),
			_leaf("c", "6-1602", "Listrik", 1.0, parent="a"),
			_leaf("b", "6-1601", "Air", 1.0, parent="a"),
			_leaf("z", "6-1100", "Gaji", 1.0),
		]
		tree = account_tree(leaves, [], ["EXPENSE"])
		self.assertEqual([n["code"] for n in tree], ["6-1100", "6-1600"])
		self.assertEqual([n["code"] for n in tree[1]["children"]], ["6-1601", "6-1602"])

	def test_flatten_tags_depth(self):
		leaves = [
			_leaf("a", "6-1600", "Ops", 0.0),
			_leaf("b", "6-1602", "Listrik", 1.0, parent="a"),
		]
		flat = flatten_tree(account_tree(leaves, [], ["EXPENSE"]))
		self.assertEqual([(f["code"], f["depth"]) for f in flat], [("6-1600", 0), ("6-1602", 1)])


# The live chart of accounts (PT POI, Oct 2026). Every one of these must map to
# a real section — a LAIN-LAIN hit means the mapping has a hole.
LIVE_COA = [
	("1-1110", "Asset", "Cash", "ASSET"), ("1-1120", "Asset", "Bank", "ASSET"),
	("1-1130", "Asset", "Bank", "ASSET"), ("1-1140", "Asset", "Bank", "ASSET"),
	("1-1150", "Asset", "Bank", "ASSET"), ("1-1160", "Asset", "Bank", "ASSET"),
	("1-1210", "Asset", "Receivable", "ASSET"), ("1-1220", "Asset", "Receivable", "ASSET"),
	("1-1310", "Asset", "", "ASSET"), ("1-1320", "Asset", "", "ASSET"),
	("1-1410", "Asset", "Stock", "ASSET"), ("1-1900", "Asset", "", "ASSET"),
	("1-2100", "Asset", "Fixed Asset", "ASSET"),
	("1-2110", "Asset", "Accumulated Depreciation", "ASSET"),
	("1-2200", "Asset", "Fixed Asset", "ASSET"),
	("1-2210", "Asset", "Accumulated Depreciation", "ASSET"),
	("2-1110", "Liability", "Payable", "LIABILITY"),
	("2-1120", "Liability", "Payable", "LIABILITY"),
	("2-1210", "Liability", "", "LIABILITY"), ("2-1220", "Liability", "", "LIABILITY"),
	("2-1310", "Liability", "Tax", "LIABILITY"), ("2-1320", "Liability", "Tax", "LIABILITY"),
	("2-1330", "Liability", "Tax", "LIABILITY"), ("2-1400", "Liability", "", "LIABILITY"),
	("2-1500", "Liability", "", "LIABILITY"), ("2-1510", "Liability", "", "LIABILITY"),
	("2-1520", "Liability", "", "LIABILITY"), ("2-1600", "Liability", "", "LIABILITY"),
	("2-1700", "Liability", "", "LIABILITY"), ("2-1800", "Liability", "", "LIABILITY"),
	("3-1100", "Equity", "", "EQUITY"), ("3-1200", "Equity", "", "EQUITY"),
	("3-1300", "Equity", "", "EQUITY"), ("3-1400", "Equity", "", "EQUITY"),
	("4-1100", "Income", "", "REVENUE"), ("4-2100", "Income", "", "REVENUE"),
	("5-1100", "Expense", "", "COGS"), ("5-1200", "Expense", "", "COGS"),
	("6-1100", "Expense", "", "EXPENSE"), ("8-1100", "Expense", "", "OTHER_EXPENSE"),
]


class CashSectionTest(unittest.TestCase):
	def test_every_live_account_maps(self):
		unmapped = [
			code for code, rt, at, ot in LIVE_COA
			if cash_section(code, rt, at, ot) == SECTION_OTHER
		]
		self.assertEqual(unmapped, [], "unmapped accounts fall into LAIN-LAIN")

	def test_financing(self):
		for code in ("2-1500", "2-1510", "2-1520", "2-1700", "2-1800"):
			self.assertEqual(cash_section(code, "Liability", "", "LIABILITY"), SECTION_PENDANAAN)
		for code in ("3-1100", "3-1400"):
			self.assertEqual(cash_section(code, "Equity", "", "EQUITY"), SECTION_PENDANAAN)

	def test_investing_covers_fixed_assets_and_depreciation(self):
		self.assertEqual(
			cash_section("1-2100", "Asset", "Fixed Asset", "ASSET"), SECTION_INVESTASI
		)
		self.assertEqual(
			cash_section("1-2210", "Asset", "Accumulated Depreciation", "ASSET"),
			SECTION_INVESTASI,
		)

	def test_operating_covers_pl_and_working_capital(self):
		self.assertEqual(cash_section("4-1100", "Income", "", "REVENUE"), SECTION_OPERASI)
		self.assertEqual(cash_section("5-1100", "Expense", "", "COGS"), SECTION_OPERASI)
		# Client DP, taxes and project advances are working capital, not financing.
		self.assertEqual(cash_section("2-1220", "Liability", "", "LIABILITY"), SECTION_OPERASI)
		self.assertEqual(cash_section("2-1310", "Liability", "Tax", "LIABILITY"), SECTION_OPERASI)
		self.assertEqual(cash_section("1-1310", "Asset", "", "ASSET"), SECTION_OPERASI)

	def test_unknown_root_type_degrades_loudly(self):
		self.assertEqual(cash_section("9-9999", None, None, None), SECTION_OTHER)


KOPRA = "1-1120 - Bank Mandiri Kopra - POI"
XPRESI = "1-1150 - Bank BCA Xpresi - POI"
REVENUE = "4-1100 - Pendapatan Jasa - POI"
ADMIN_FEE = "6-1700 - Beban Administrasi Bank - POI"
LOAN = "2-1700 - Pinjaman Bank - POI"

META = {
	KOPRA: {"code": "1-1120", "name": "Bank Mandiri Kopra", "root_type": "Asset",
	        "account_type": "Bank", "orion_type": "ASSET"},
	XPRESI: {"code": "1-1150", "name": "Bank BCA Xpresi", "root_type": "Asset",
	         "account_type": "Bank", "orion_type": "ASSET"},
	REVENUE: {"code": "4-1100", "name": "Pendapatan Jasa", "root_type": "Income",
	          "account_type": "", "orion_type": "REVENUE"},
	ADMIN_FEE: {"code": "6-1700", "name": "Beban Administrasi Bank", "root_type": "Expense",
	            "account_type": "", "orion_type": "EXPENSE"},
	LOAN: {"code": "2-1700", "name": "Pinjaman Bank", "root_type": "Liability",
	       "account_type": "", "orion_type": "LIABILITY"},
}
CASH = {KOPRA, XPRESI}


def _leg(vno, account, debit=0.0, credit=0.0, ym=202601, vtype="Journal Entry", in_period=True):
	return {
		"voucher_type": vtype, "voucher_no": vno, "account": account,
		"debit": debit, "credit": credit, "ym": ym, "in_period": in_period,
	}


class CashFlowTest(unittest.TestCase):
	def test_two_leg_voucher_is_exact(self):
		legs = [
			_leg("JE-1", KOPRA, debit=10_000_000),
			_leg("JE-1", REVENUE, credit=10_000_000),
		]
		out = cash_flow(legs, CASH, META, [202601], opening=5_000_000.0)
		self.assertEqual(out["mutasiBersih"], 10_000_000.0)
		self.assertEqual(out["saldoAkhir"], 15_000_000.0)
		operasi = next(s for s in out["sections"] if s["key"] == SECTION_OPERASI)
		self.assertEqual(operasi["total"], 10_000_000.0)

	def test_three_leg_bank_fee_splits_honestly(self):
		"""The case pro-rata allocation would mangle: a drawdown net of a fee.
		Financing must read the gross 5.050.000 and operating -50.000."""
		legs = [
			_leg("JE-2", KOPRA, debit=5_000_000),
			_leg("JE-2", ADMIN_FEE, debit=50_000),
			_leg("JE-2", LOAN, credit=5_050_000),
		]
		out = cash_flow(legs, CASH, META, [202601], opening=0.0)
		by_key = {s["key"]: s["total"] for s in out["sections"]}
		self.assertEqual(by_key[SECTION_PENDANAAN], 5_050_000.0)
		self.assertEqual(by_key[SECTION_OPERASI], -50_000.0)
		# And the sections still sum to the actual cash movement.
		self.assertEqual(out["mutasiBersih"], 5_000_000.0)
		self.assertEqual(out["saldoAkhir"], 5_000_000.0)

	def test_cash_to_cash_transfer_contributes_zero(self):
		legs = [
			_leg("JE-3", KOPRA, debit=3_000_000),
			_leg("JE-3", XPRESI, credit=3_000_000),
		]
		out = cash_flow(legs, CASH, META, [202601], opening=1_000_000.0)
		self.assertEqual(out["mutasiBersih"], 0.0)
		self.assertEqual(out["saldoAkhir"], 1_000_000.0)
		# The three standard sections are still presented, each at zero — a
		# statement should show "Operasi: 0", not drop the line.
		self.assertEqual([s["key"] for s in out["sections"]], list(SECTION_ORDER[:3]))
		self.assertTrue(all(s["total"] == 0.0 and not s["accounts"] for s in out["sections"]))
		self.assertEqual(out["transfersTotal"], 0.0)
		self.assertEqual(len(out["transfers"]), 2)

	def test_mixed_transfer_and_counterpart(self):
		"""Dr BCA 1.000.000 / Cr Kopra 300.000 / Cr Pendapatan 700.000 — cash
		moves +700.000 and the internal 300.000 stays invisible."""
		legs = [
			_leg("JE-4", XPRESI, debit=1_000_000),
			_leg("JE-4", KOPRA, credit=300_000),
			_leg("JE-4", REVENUE, credit=700_000),
		]
		out = cash_flow(legs, CASH, META, [202601], opening=0.0)
		self.assertEqual(out["mutasiBersih"], 700_000.0)
		operasi = next(s for s in out["sections"] if s["key"] == SECTION_OPERASI)
		self.assertEqual(operasi["total"], 700_000.0)

	def test_monthly_chain_is_continuous(self):
		"""Free invariant: saldoAwal of month n+1 == saldoAkhir of month n."""
		legs = [
			_leg("JE-5", KOPRA, debit=1_000_000, ym=202601),
			_leg("JE-5", REVENUE, credit=1_000_000, ym=202601),
			_leg("JE-7", KOPRA, credit=400_000, ym=202603),
			_leg("JE-7", ADMIN_FEE, debit=400_000, ym=202603),
		]
		months = [202601, 202602, 202603]
		out = cash_flow(legs, CASH, META, months, opening=2_000_000.0)
		bm = out["byMonth"]
		for a, b in pairwise(months):
			self.assertEqual(bm[b]["saldoAwal"], bm[a]["saldoAkhir"])
		# February was silent; the balance must carry, not reset.
		self.assertEqual(bm[202602]["neto"], 0.0)
		self.assertEqual(bm[202602]["saldoAkhir"], 3_000_000.0)
		self.assertEqual(bm[202603]["saldoAkhir"], 2_600_000.0)
		self.assertEqual(out["saldoAkhir"], bm[202603]["saldoAkhir"])

	def test_per_account_block_reconciles_to_movement(self):
		legs = [
			_leg("JE-8", KOPRA, debit=1_000_000),
			_leg("JE-8", REVENUE, credit=1_000_000),
			_leg("JE-9", XPRESI, credit=250_000),
			_leg("JE-9", ADMIN_FEE, debit=250_000),
		]
		out = cash_flow(legs, CASH, META, [202601], opening=0.0)
		self.assertEqual(
			sum(a["mutasi"] for a in out["perAccount"]), out["mutasiBersih"]
		)

	def test_suspense_is_broken_out_not_folded(self):
		suspense_acct = "1-1900 - Bank Reconciliation Suspense - POI"
		meta = dict(META)
		meta[suspense_acct] = {
			"code": "1-1900", "name": "Bank Reconciliation Suspense",
			"root_type": "Asset", "account_type": "", "orion_type": "ASSET",
		}
		legs = [
			_leg("JE-10", KOPRA, debit=6_249_300),
			_leg("JE-10", suspense_acct, credit=6_249_300),
		]
		out = cash_flow(legs, CASH, meta, [202601], opening=0.0)
		self.assertEqual(out["suspense"]["net"], 6_249_300.0)
		# Not hidden inside operating working capital...
		operasi = next(s for s in out["sections"] if s["key"] == SECTION_OPERASI)
		self.assertEqual(operasi["accounts"], [])
		self.assertEqual(operasi["total"], 0.0)
		# ...but still part of the arithmetic, so the tie-out holds.
		self.assertEqual(out["saldoAkhir"], 6_249_300.0)

	def test_unbalanced_voucher_is_reported(self):
		legs = [
			_leg("JE-11", KOPRA, debit=1_000_000),
			_leg("JE-11", REVENUE, credit=900_000),
		]
		out = cash_flow(legs, CASH, META, [202601], opening=0.0)
		self.assertEqual(len(out["unbalancedVouchers"]), 1)
		self.assertEqual(out["unbalancedVouchers"][0]["voucherNo"], "JE-11")

	def test_leg_outside_period_is_quarantined(self):
		legs = [
			_leg("JE-12", KOPRA, debit=500_000),
			_leg("JE-12", REVENUE, credit=500_000, ym=202512, in_period=False),
		]
		out = cash_flow(legs, CASH, META, [202601], opening=0.0)
		self.assertEqual(len(out["outsidePeriod"]), 1)

	def test_non_je_voucher_types_are_included(self):
		"""Payment Entries and Sales Invoices are 7% of live GL rows."""
		legs = [
			_leg("PE-1", KOPRA, debit=2_000_000, vtype="Payment Entry"),
			_leg("PE-1", REVENUE, credit=2_000_000, vtype="Payment Entry"),
		]
		out = cash_flow(legs, CASH, META, [202601], opening=0.0)
		self.assertEqual(out["mutasiBersih"], 2_000_000.0)


class AgingBucketTest(unittest.TestCase):
	def test_bands(self):
		self.assertEqual(aging_bucket(-5), "Belum jatuh tempo")
		self.assertEqual(aging_bucket(0), "0-30 hari")
		self.assertEqual(aging_bucket(30), "0-30 hari")
		self.assertEqual(aging_bucket(31), "31-60 hari")
		self.assertEqual(aging_bucket(60), "31-60 hari")
		self.assertEqual(aging_bucket(90), "61-90 hari")
		self.assertEqual(aging_bucket(91), "> 90 hari")


if __name__ == "__main__":
	unittest.main()
