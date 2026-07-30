"""Kontrak/KN project-code helpers.

Code format: {R|P}-KN{nn}-{YY}{roman month}-{LCTR}-{LAYANAN}
    e.g. R-KN07-26III-WNGT-RENOV

Ports two sources:

  feynman src/lib/kn.ts           -- comparator/decomposition (knSortKey,
                                     compareKnCode, knYearOptions). A plain
                                     string sort breaks two ways: KN10 lands
                                     before KN2, and the running number resets
                                     every year, so 2025's KN20 would outrank
                                     2026's KN01. Decompose into
                                     year -> seq -> month -> line instead.
  bellatrix Node code generation  -- routes/crm/index.ts (POST /api/contracts,
                                     GET /api/contracts/kn-next) and
                                     routes/supply-chain/projects.ts (POST):
                                     KN running numbers are per business line
                                     (RATUNDA and POIESIS run independent
                                     sequences) and reset per year; the year is
                                     scoped by the two-digit prefix of
                                     kontrakYYMM; next = MAX(knSequence) + 1.

Everything except `next_kn_sequence` is pure (no frappe import) so the module
stays unit-testable outside a bench — same layout as loan_codes.py.
"""

import re

ROMAN = [
	"I", "II", "III", "IV", "V", "VI",
	"VII", "VIII", "IX", "X", "XI", "XII",
]
ROMAN_VALUE = {r: i + 1 for i, r in enumerate(ROMAN)}

LINE_PREFIX = {
	"RATUNDA_RENOVASI": "R",
	"POIESIS_STUDIO": "P",
}
LINE_BY_PREFIX = {v: k for k, v in LINE_PREFIX.items()}

_PERIOD_RE = re.compile(r"^(\d{2})([IVX]+)$")
_SEQ_RE = re.compile(r"\d+")
_CODE_RE = re.compile(
	r"^(?P<prefix>[RP])-KN(?P<seq>\d+)-(?P<yy>\d{2})(?P<roman>[IVX]+)"
	r"-(?P<lctr>[A-Z0-9]{4})-(?P<svc>[A-Z0-9]+)$"
)


def roman_month(month):
	"""1-12 (or a date/datetime) -> Roman numeral, VII for July."""
	if hasattr(month, "month"):
		month = month.month
	return ROMAN[month - 1]


def month_from_roman(roman):
	"""'VII' -> 7; 0 when not a valid month numeral (mirrors kn.ts)."""
	return ROMAN_VALUE.get(roman, 0)


def kontrak_yymm(when):
	"""date/datetime -> the period segment, e.g. '26VII' for 2026-07."""
	return "%s%s" % (when.strftime("%y"), roman_month(when))


def build_code(business_line, seq, yy, roman, lctr, svc):
	"""Render one kontrak code. Pure.

	>>> build_code("RATUNDA_RENOVASI", 7, 26, "III", "WNGT", "RENOV")
	'R-KN07-26III-WNGT-RENOV'

	`yy` may be an int (26) or two-digit string ("26"); `roman` is the month
	numeral. The Node builder pads seq to two digits but never truncates, so
	seq 100 renders KN100.
	"""
	prefix = LINE_PREFIX.get(business_line, "R")
	return "%s-KN%02d-%02d%s-%s-%s" % (prefix, int(seq), int(yy), roman, lctr, svc)


def parse_code(code):
	"""Strict decomposition of a well-formed kontrak code, or None.

	Returns {line, business_line, seq, yy, month, roman, lctr,
	service_type_code}. Use `sort_key` for lenient/partial codes.
	"""
	m = _CODE_RE.match(code or "")
	if not m:
		return None
	month = month_from_roman(m.group("roman"))
	if not month:
		return None
	return {
		"line": m.group("prefix"),
		"business_line": LINE_BY_PREFIX[m.group("prefix")],
		"seq": int(m.group("seq")),
		"yy": int(m.group("yy")),
		"month": month,
		"roman": m.group("roman"),
		"lctr": m.group("lctr"),
		"service_type_code": m.group("svc"),
	}


def sort_key(code):
	"""kn.ts knSortKey: (year, seq, month, line), zeros when unparseable.

	Tuples compare exactly like the kn.ts comparator chain
	(year - seq - month - line), so `sorted(codes, key=sort_key)` reproduces
	compareKnCode ordering.
	"""
	seg = (code or "").split("-")
	period = _PERIOD_RE.match(seg[2] if len(seg) > 2 else "")
	seq = _SEQ_RE.search(seg[1] if len(seg) > 1 else "")
	return (
		int(period.group(1)) if period else 0,
		int(seq.group(0)) if seq else 0,
		month_from_roman(period.group(2)) if period else 0,
		seg[0] if seg else "",
	)


def compare_codes(a, b):
	"""Ascending comparator, kn.ts compareKnCode: -1/0/1."""
	ka, kb = sort_key(a), sort_key(b)
	return (ka > kb) - (ka < kb)


def year_options(codes):
	"""Distinct two-digit years present in `codes`, newest first
	(kn.ts knYearOptions). Unparseable codes contribute nothing."""
	years = {sort_key(c)[0] for c in codes}
	years.discard(0)
	return sorted(years, reverse=True)


def next_kn_sequence(business_line, year=None):
	"""Next per-line, per-year KN running number, max-scan over
	Project.kn_sequence (plan: 'Generator ported to orion.accounting.kn
	(max-scan)').

	Node source (crm/index.ts kn-next, supply-chain/projects.ts POST):
	MAX(knSequence) over projects of the same businessLine whose kontrakYYMM
	starts with the two-digit year, + 1; 1 when none exist. `year` accepts a
	4- or 2-digit int/str; defaults to the current year. Frappe imported
	lazily so the pure helpers stay importable without a bench.
	"""
	import frappe

	if year is None:
		from datetime import date

		year = date.today().year
	yy = "%02d" % (int(year) % 100)
	line = "POIESIS_STUDIO" if business_line == "POIESIS_STUDIO" else "RATUNDA_RENOVASI"
	rows = frappe.get_all(
		"Project",
		filters={
			"orion_business_line": line,
			"kontrak_yymm": ("like", yy + "%"),
			"kn_sequence": ("is", "set"),
		},
		fields=["max(kn_sequence) as last"],
	)
	last = rows[0].last if rows and rows[0].last else 0
	return int(last) + 1
