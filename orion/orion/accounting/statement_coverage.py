"""Monthly bank-statement coverage: has each company account's statement for a
given month been fully imported and tagged to the CoA?

The completion signal is deliberately NOT "are there any ledger rows" — the
ledger alone can never prove a month is *whole* (you don't know how many
transactions the bank actually had). Instead it keys off the user's real
workflow: they upload ONE monthly rekening-koran PDF per account per month
through the Import Rekening Koran screen (source="manual") and tag every line
to a CoA account, which posts BANK_IMPORT Journal Entries. So:

  completed  — a manual (monthly) statement job exists for the account+month
               AND all its non-duplicate lines are recorded as BANK_IMPORT JEs
               (i.e. every line has been tagged and saved).
  partial    — Kopra only in the normal case: the daily inbox (Octopus) feed
               has recorded some days but the month has not been closed with a
               full-month import. Also covers the transient mid-tagging state
               (statement uploaded, only some lines saved) on any account.
  not_started— nothing recorded and no monthly statement uploaded.

Only the Mandiri Kopra account has the daily inbox feed (see DAILY_INBOX_ACCOUNTS);
every other operational account is only ever green or grey.

Results are stored per (account, month) in the "Orion Bank Statement Coverage"
doctype so the ledger is not rescanned on every page open. Cells are computed
lazily on first read, recomputed automatically when a BANK_IMPORT batch is
posted (mark_dirty, called from the JE write paths), and can be force-recomputed
from the UI.
"""

import json
from calendar import monthrange
from datetime import date

import frappe

from orion.accounting.account_mapper import OPERATIONAL_ACCOUNTS

COVERAGE_DOCTYPE = "Orion Bank Statement Coverage"
JOB_DOCTYPE = "Orion Bank Statement Job"

# Only the Mandiri Kopra account has the daily Octopus inbox feed, so it is the
# only account that legitimately accretes a month day-by-day (partial status).
DAILY_INBOX_ACCOUNTS = {"1660007677776"}

STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_NOT_STARTED = "not_started"

# Short Indonesian month labels for the UI column headers.
_MONTH_ABBR_ID = {
	1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "Mei", 6: "Jun",
	7: "Jul", 8: "Agu", 9: "Sep", 10: "Okt", 11: "Nov", 12: "Des",
}


# ── month helpers ────────────────────────────────────────────────────────────


def _month_bounds(year: int, month: int) -> tuple[str, str]:
	last = monthrange(year, month)[1]
	return "%04d-%02d-01" % (year, month), "%04d-%02d-%02d" % (year, month, last)


def month_label(year: int, month: int) -> str:
	return "%s %02d" % (_MONTH_ABBR_ID.get(month, str(month)), year % 100)


def recent_months(count: int = 6, today: date | None = None) -> list[dict]:
	"""Current month + the previous (count-1) months, oldest first."""
	if today is None:
		today = frappe.utils.getdate(frappe.utils.nowdate())
	y, m = today.year, today.month
	out = []
	for _ in range(count):
		out.append({"year": y, "month": m, "label": month_label(y, m)})
		m -= 1
		if m == 0:
			m = 12
			y -= 1
	out.reverse()
	return out


# ── recorded ledger side ─────────────────────────────────────────────────────


def _recorded(account_no: str, year: int, month: int) -> tuple[int, int, set]:
	"""(count, distinct-days, txn-hash set) of BANK_IMPORT JEs posted for this
	account in this month. docstatus 1 only (submitted, not cancelled)."""
	start, end = _month_bounds(year, month)
	rows = frappe.get_all(
		"Journal Entry",
		filters={
			"orion_source_type": "BANK_IMPORT",
			"orion_bank_account_no": account_no,
			"docstatus": 1,
			"posting_date": ("between", [start, end]),
		},
		fields=["posting_date", "orion_txn_hash"],
	)
	hashes = {r.orion_txn_hash for r in rows if r.orion_txn_hash}
	days = {str(r.posting_date)[:10] for r in rows}
	return len(rows), len(days), hashes


# ── statement-job (monthly upload) side ──────────────────────────────────────


def _job_month_txns(result: dict, year: int, month: int) -> tuple[int, set, int, int]:
	"""(non-dup count, non-dup hash set, min-day, max-day) for the job's
	transactions that fall inside this calendar month."""
	prefix = "%04d-%02d" % (year, month)
	count, hashes, days = 0, set(), []
	for tx in result.get("transactions") or []:
		d = str(tx.get("date") or "")[:10]
		if d[:7] != prefix:
			continue
		if tx.get("isDuplicate"):
			continue
		count += 1
		if tx.get("txnHash"):
			hashes.add(tx["txnHash"])
		try:
			days.append(int(d[8:10]))
		except ValueError:
			pass
	return count, hashes, (min(days) if days else 0), (max(days) if days else 0)


def _covers_whole_month(min_day: int, max_day: int, year: int, month: int) -> bool:
	"""A statement whose transactions span from near the 1st to near month-end
	is a full-month statement (vs a single-day daily dump)."""
	if not min_day or not max_day:
		return False
	last = monthrange(year, month)[1]
	return min_day <= 5 and max_day >= last - 4


def _full_month_job(account_no: str, year: int, month: int):
	"""The statement job that represents this account's full month, or None.

	A manual-source job is always a monthly upload (the user never uploads
	dailies by hand). An auto-source job counts only when its transactions span
	the whole month (the Octopus "Last Month" close) — daily auto dumps do not.
	Returns (job_name, period_label, expected_count, expected_hashes) or None.
	"""
	jobs = frappe.get_all(
		JOB_DOCTYPE,
		filters={"status": "done", "bank_account_no": account_no},
		fields=["name", "source", "result", "file_name"],
		order_by="creation desc",
	)
	for job in jobs:
		if not job.result:
			continue
		try:
			result = json.loads(job.result)
		except (ValueError, TypeError):
			continue
		count, hashes, min_day, max_day = _job_month_txns(result, year, month)
		if count == 0:
			continue
		period = ((result.get("info") or {}).get("period")) or ""
		# A manual job is a monthly upload by definition (the user only uploads
		# whole-month PDFs by hand). An auto job counts only when its transactions
		# span the whole month (the Octopus "Last Month" close) — a daily auto
		# dump spans a single day and must NOT qualify. The free-form period
		# string is deliberately NOT trusted here: a daily statement's period
		# ("28 July 2026 - 28 July 2026") names the month and would falsely read
		# as a full month.
		is_monthly = job.source == "manual" or _covers_whole_month(
			min_day, max_day, year, month
		)
		if is_monthly:
			return job.name, period, count, hashes
	return None


# ── compute + persist a single cell ──────────────────────────────────────────


def compute_cell(account_no: str, year: int, month: int) -> dict:
	"""Pure computation for one (account, month) cell — no persistence."""
	recorded_count, recorded_days, recorded_hashes = _recorded(account_no, year, month)
	is_inbox = account_no in DAILY_INBOX_ACCOUNTS

	job = _full_month_job(account_no, year, month)
	full_month_job = period_label = None
	expected_count = 0

	if job:
		full_month_job, period_label, expected_count, expected_hashes = job
		# Primary signal: every non-duplicate statement line is recorded (its
		# txn hash present on a posted JE). Count fallback covers the rare case
		# where posting suffixed a within-batch hash collision (|N), so the
		# stored hash differs from the statement's — as many recorded as expected.
		fully_recorded = expected_count > 0 and (
			expected_hashes.issubset(recorded_hashes)
			or recorded_count >= expected_count
		)
		if fully_recorded:
			status = STATUS_COMPLETED
		elif recorded_count > 0:
			status = STATUS_PARTIAL
		else:
			status = STATUS_NOT_STARTED
	elif is_inbox and recorded_count > 0:
		# Kopra: daily inbox has recorded some days, month not yet closed.
		status = STATUS_PARTIAL
	elif recorded_count > 0:
		status = STATUS_PARTIAL
	else:
		status = STATUS_NOT_STARTED

	return {
		"accountNo": account_no,
		"year": year,
		"month": month,
		"status": status,
		"recordedCount": recorded_count,
		"recordedDays": recorded_days,
		"expectedCount": expected_count,
		"fullMonthJob": full_month_job,
		"periodLabel": period_label,
		"hasInbox": is_inbox,
	}


def _store(cell: dict) -> None:
	name = frappe.db.get_value(
		COVERAGE_DOCTYPE,
		{
			"account_no": cell["accountNo"],
			"period_year": cell["year"],
			"period_month": cell["month"],
		},
	)
	values = {
		"status": cell["status"],
		"recorded_count": cell["recordedCount"],
		"recorded_days": cell["recordedDays"],
		"expected_count": cell["expectedCount"],
		"full_month_job": cell["fullMonthJob"],
		"period_label": cell["periodLabel"],
		"has_inbox": 1 if cell["hasInbox"] else 0,
		"computed_at": frappe.utils.now_datetime(),
	}
	if name:
		frappe.db.set_value(COVERAGE_DOCTYPE, name, values, update_modified=True)
	else:
		doc = frappe.new_doc(COVERAGE_DOCTYPE)
		doc.account_no = cell["accountNo"]
		doc.period_year = cell["year"]
		doc.period_month = cell["month"]
		doc.update(values)
		doc.flags.ignore_permissions = True
		doc.insert()


def _stored(account_no: str, year: int, month: int) -> dict | None:
	row = frappe.db.get_value(
		COVERAGE_DOCTYPE,
		{"account_no": account_no, "period_year": year, "period_month": month},
		[
			"status", "recorded_count", "recorded_days", "expected_count",
			"full_month_job", "period_label", "has_inbox",
		],
		as_dict=True,
	)
	if not row:
		return None
	return {
		"accountNo": account_no,
		"year": year,
		"month": month,
		"status": row.status,
		"recordedCount": row.recorded_count,
		"recordedDays": row.recorded_days,
		"expectedCount": row.expected_count,
		"fullMonthJob": row.full_month_job,
		"periodLabel": row.period_label,
		"hasInbox": bool(row.has_inbox),
	}


def get_or_compute(account_no: str, year: int, month: int) -> dict:
	"""Return the stored cell; compute + persist it on first access."""
	cell = _stored(account_no, year, month)
	if cell is not None:
		return cell
	cell = compute_cell(account_no, year, month)
	_store(cell)
	return cell


def recompute(account_no: str, year: int, month: int) -> dict:
	cell = compute_cell(account_no, year, month)
	_store(cell)
	return cell


# ── company account registry (only company accounts are shown) ───────────────


def company_accounts() -> list[dict]:
	"""PT POI's operational bank accounts — the only accounts that receive
	statements — enriched with bank name + holder from the Bank Account doctype."""
	ba_map = {}
	for ba in frappe.get_all(
		"Bank Account",
		filters={"is_company_account": 1},
		fields=["bank_account_no", "bank", "account_name"],
	):
		if ba.bank_account_no:
			ba_map[ba.bank_account_no] = ba
	out = []
	for account_no, gl_code, friendly in OPERATIONAL_ACCOUNTS:
		ba = ba_map.get(account_no)
		out.append(
			{
				"accountNo": account_no,
				"glCode": gl_code,
				"bankName": (ba.bank if ba else None) or friendly,
				"owner": (ba.account_name if ba else None) or friendly,
				"hasInbox": account_no in DAILY_INBOX_ACCOUNTS,
			}
		)
	return out


# ── dirty-marking hook (called from the JE write paths) ──────────────────────


def mark_dirty(account_no: str, date_keys) -> None:
	"""Recompute the coverage cells for the (account, month)s touched by a freshly
	posted BANK_IMPORT batch. Best-effort: never raise into the caller — coverage
	is advisory and must not break journal posting."""
	if not account_no or not date_keys:
		return
	try:
		months = set()
		for d in date_keys:
			s = str(d)[:7]
			if len(s) == 7 and s[4] == "-":
				months.add((int(s[:4]), int(s[5:7])))
		for year, month in months:
			recompute(account_no, year, month)
	except Exception:
		frappe.log_error(
			title="Bank statement coverage recompute failed",
			message=frappe.get_traceback(),
		)
