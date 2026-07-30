"""Tests for orion.accounting.kn (pure parts only — no frappe).

feynman's kn.ts ships no test file, so expectations are written against the
documented behavior of knSortKey/compareKnCode/knYearOptions and real codes
from the production data (R-KN07-26III-WNGT-RENOV et al).
"""

import unittest

from orion.accounting.kn import (
	build_code,
	compare_codes,
	kontrak_yymm,
	month_from_roman,
	parse_code,
	roman_month,
	sort_key,
	year_options,
)


class TestBuildCode(unittest.TestCase):
	def test_real_ratunda_code(self):
		self.assertEqual(
			build_code("RATUNDA_RENOVASI", 7, 26, "III", "WNGT", "RENOV"),
			"R-KN07-26III-WNGT-RENOV",
		)

	def test_real_poiesis_code(self):
		self.assertEqual(
			build_code("POIESIS_STUDIO", 1, 25, "XII", "PAKN", "DSGN"),
			"P-KN01-25XII-PAKN-DSGN",
		)

	def test_sequence_zero_padded_but_never_truncated(self):
		self.assertEqual(
			build_code("RATUNDA_RENOVASI", 100, 26, "I", "ABCD", "RENOV"),
			"R-KN100-26I-ABCD-RENOV",
		)

	def test_unknown_line_defaults_to_ratunda_prefix(self):
		# Node: linePrefix = POIESIS_STUDIO ? 'P' : 'R'
		self.assertTrue(build_code(None, 1, 26, "I", "ABCD", "SVC").startswith("R-"))

	def test_string_year_accepted(self):
		self.assertEqual(
			build_code("RATUNDA_RENOVASI", 7, "26", "VII", "WNGT", "RENOV"),
			"R-KN07-26VII-WNGT-RENOV",
		)


class TestParseCode(unittest.TestCase):
	def test_round_trip(self):
		parsed = parse_code("R-KN07-26III-WNGT-RENOV")
		self.assertEqual(
			parsed,
			{
				"line": "R",
				"business_line": "RATUNDA_RENOVASI",
				"seq": 7,
				"yy": 26,
				"month": 3,
				"roman": "III",
				"lctr": "WNGT",
				"service_type_code": "RENOV",
			},
		)
		self.assertEqual(
			build_code(
				parsed["business_line"], parsed["seq"], parsed["yy"],
				parsed["roman"], parsed["lctr"], parsed["service_type_code"],
			),
			"R-KN07-26III-WNGT-RENOV",
		)

	def test_poiesis_prefix(self):
		self.assertEqual(
			parse_code("P-KN02-26VII-TSHS-DSGN")["business_line"], "POIESIS_STUDIO"
		)

	def test_rejects_legacy_and_garbage(self):
		for bad in (None, "", "RR-2025-001", "R-KN07-26XIII-WNGT-RENOV",
				"X-KN07-26III-WNGT-RENOV", "R-KN07-26III-WNG-RENOV"):
			self.assertIsNone(parse_code(bad), bad)


class TestSortKey(unittest.TestCase):
	def test_kn10_outranks_kn2_same_year(self):
		# the string-sort failure mode kn.ts exists to fix
		self.assertGreater(
			sort_key("R-KN10-26III-AAAA-RENOV"), sort_key("R-KN02-26XII-BBBB-RENOV")
		)

	def test_new_year_kn01_outranks_old_year_kn20(self):
		self.assertGreater(
			sort_key("R-KN01-26I-AAAA-RENOV"), sort_key("R-KN20-25XII-BBBB-RENOV")
		)

	def test_month_breaks_ties_then_line(self):
		self.assertLess(
			sort_key("R-KN07-26III-AAAA-RENOV"), sort_key("R-KN07-26IV-AAAA-RENOV")
		)
		self.assertLess(
			sort_key("P-KN07-26III-AAAA-DSGN"), sort_key("R-KN07-26III-AAAA-RENOV")
		)

	def test_unparseable_decomposes_to_zeros(self):
		self.assertEqual(sort_key(None), (0, 0, 0, ""))
		# kn.ts still digs digits out of segment 1 (year stays 0, so legacy
		# codes sort below every well-formed KN code regardless)
		self.assertEqual(sort_key("RR-2025-001"), (0, 2025, 0, "RR"))

	def test_sorted_matches_compare(self):
		codes = [
			"R-KN10-26III-AAAA-RENOV",
			"R-KN02-26XII-BBBB-RENOV",
			"P-KN01-25XII-PAKN-DSGN",
			"R-KN07-26III-WNGT-RENOV",
		]
		self.assertEqual(
			sorted(codes, key=sort_key),
			[
				"P-KN01-25XII-PAKN-DSGN",
				"R-KN02-26XII-BBBB-RENOV",
				"R-KN07-26III-WNGT-RENOV",
				"R-KN10-26III-AAAA-RENOV",
			],
		)
		self.assertEqual(compare_codes("R-KN02-26I-A-B", "R-KN10-26I-A-B"), -1)
		self.assertEqual(compare_codes("R-KN07-26III-WNGT-RENOV", "R-KN07-26III-WNGT-RENOV"), 0)


class TestRomanHelpers(unittest.TestCase):
	def test_all_twelve_months_round_trip(self):
		for m in range(1, 13):
			self.assertEqual(month_from_roman(roman_month(m)), m)

	def test_roman_month_accepts_dates(self):
		from datetime import date

		self.assertEqual(roman_month(date(2026, 7, 29)), "VII")

	def test_invalid_numeral_is_zero(self):
		self.assertEqual(month_from_roman("XIII"), 0)
		self.assertEqual(month_from_roman(""), 0)

	def test_kontrak_yymm(self):
		from datetime import date

		self.assertEqual(kontrak_yymm(date(2026, 3, 1)), "26III")


class TestYearOptions(unittest.TestCase):
	def test_distinct_years_newest_first_unparseable_dropped(self):
		codes = [
			"R-KN07-26III-WNGT-RENOV",
			"P-KN01-25XII-PAKN-DSGN",
			"R-KN02-26XII-BBBB-RENOV",
			"RR-2025-001",
			None,
		]
		self.assertEqual(year_options(codes), [26, 25])


if __name__ == "__main__":
	unittest.main()
