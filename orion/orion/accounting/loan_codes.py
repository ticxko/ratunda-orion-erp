"""Document-code generation and balance derivation for loan events.

Port of `app/services/accounting/loan_codes.py` (plus `recompute_outstanding`
from `app/models/accounting/loan.py`) from Bellatrix Python. The codes mirror
the ones already used over email for PT POI capital loans, so Orion replaces
the manual PDF process rather than competing with it:

    drawdown  ->  PTPOI-CL04-26VII        (Capital Loan #04, 2026 month VII)
    repayment ->  POI-PB03-26VI-PTPOI     (Payback #03, 2026 month VI)

The running number is per-loan and per-direction, and it is continuous across
months (the real CL01..CL08 series spans June into July).

`roman_month`, `build_doc_code` and `recompute_outstanding` are pure; the
`next_*` helpers hit the database and import frappe lazily so the pure parts
stay unit-testable outside a bench.
"""

from decimal import Decimal

ROMAN = [
	"I", "II", "III", "IV", "V", "VI",
	"VII", "VIII", "IX", "X", "XI", "XII",
]

DEFAULT_DRAWDOWN_PREFIX = "PTPOI-CL"
DEFAULT_REPAYMENT_PREFIX = "POI-PB"


def roman_month(when):
	"""Month of a date/datetime as a Roman numeral (VII for July)."""
	return ROMAN[when.month - 1]


def build_doc_code(*, direction, sequence, when, prefix=None):
	"""Render one loan-event document code. Pure."""
	yy = when.strftime("%y")
	mon = roman_month(when)
	if direction == "DRAWDOWN":
		head = prefix or DEFAULT_DRAWDOWN_PREFIX
		return "%s%02d-%s%s" % (head, sequence, yy, mon)
	head = prefix or DEFAULT_REPAYMENT_PREFIX
	return "%s%02d-%s%s-PTPOI" % (head, sequence, yy, mon)


def recompute_outstanding(events):
	"""Outstanding = sum(drawdowns) - sum(repayments), floored at zero.

	`events` is an iterable of (direction, principal_amount) pairs. Never
	increment the balance in place; always derive it from the events so that
	reversals and out-of-order edits stay consistent.
	"""
	total = Decimal("0")
	for direction, amount in events:
		amount = amount if isinstance(amount, Decimal) else Decimal(str(amount or 0))
		if direction == "DRAWDOWN":
			total += amount
		else:
			total -= amount
	return max(Decimal("0"), total)


def next_sequence(loan, direction):
	"""Next running number for this loan+direction.

	Counts existing events rather than parsing codes, so manually-coded rows
	(e.g. the historical CL01/CL04/CL05/CL08 seed) still advance the series.
	"""
	import frappe

	used = frappe.db.count("Orion Loan Event", {"loan": loan, "direction": direction})
	return (used or 0) + 1


def next_doc_code(*, loan, direction, when, prefix=None):
	"""Generate the next free doc code, skipping any already taken."""
	import frappe

	seq = next_sequence(loan, direction)
	for _ in range(100):
		code = build_doc_code(direction=direction, sequence=seq, when=when, prefix=prefix)
		if not frappe.db.exists("Orion Loan Event", {"doc_code": code}):
			return code
		seq += 1
	raise RuntimeError("Could not allocate a free loan document code")
