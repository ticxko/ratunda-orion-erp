"""Per-month installment schedule generation for fixed-installment loans.

Used for OWNER_PERSONAL (owner's personal KTA carried off PT POI's books, e.g.
DBS / BFI) and real BANK loans — anything with a fixed monthly installment,
principal and tenor. Pure and frappe-free so it stays unit-testable outside a
bench, like `loan_codes.py`.

Model: total each month is the fixed `monthly_installment`. Principal is spread
evenly (floor), the rounding remainder lands on the final installment so the
principal portions sum to `principal` exactly. Interest per row is the balancing
figure `total - principal` (floored at zero). For DBS (principal 150,000,000,
installment 7,011,666, tenor 36) this yields principal 4,166,666 / interest
2,845,000 per month, matching the real KTA amortisation.
"""

import calendar
from datetime import date, datetime
from decimal import Decimal


def _as_date(value):
	"""Accept a date/datetime or an ISO 'YYYY-MM-DD' string."""
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def add_months(when, k):
	"""`when` + k calendar months, clamping the day to the target month end."""
	when = _as_date(when)
	m = when.month - 1 + k
	year = when.year + m // 12
	month = m % 12 + 1
	day = min(when.day, calendar.monthrange(year, month)[1])
	return date(year, month, day)


def build_schedule(*, start_date, tenor_months, monthly_installment, principal, first_due_date=None):
	"""Return a list of installment dicts, 1-indexed.

	Each item: {installment_no, due_date (ISO), due_principal, due_interest,
	due_total} as Decimals. `first_due_date` defaults to `start_date` (installment
	1 falls on the start date; the caller sets start_date to the first payment
	date when the payment day differs from origination).
	"""
	tenor = int(tenor_months or 0)
	if tenor <= 0:
		return []
	total = Decimal(str(monthly_installment or 0))
	principal = Decimal(str(principal or 0))
	first_due = _as_date(first_due_date or start_date)

	base_principal = (principal / tenor).to_integral_value(rounding="ROUND_FLOOR")
	rows = []
	principal_so_far = Decimal("0")
	for n in range(1, tenor + 1):
		if n < tenor:
			due_principal = base_principal
		else:
			# Final row absorbs the rounding remainder so principal sums exactly.
			due_principal = principal - principal_so_far
		principal_so_far += due_principal
		due_interest = total - due_principal
		if due_interest < 0:
			due_interest = Decimal("0")
		rows.append(
			{
				"installment_no": n,
				"due_date": add_months(first_due, n - 1).isoformat(),
				"due_principal": due_principal,
				"due_interest": due_interest,
				"due_total": due_principal + due_interest,
			}
		)
	return rows
