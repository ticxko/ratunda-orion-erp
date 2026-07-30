"""Accounting compat handlers.

Mirrors the bellatrix-python routers under app/routers/accounting/ so the
feynman accounting screens work unchanged against ERPNext data:

  /api/accounting/accounts            <- accounts.py           (GET list)
  /api/accounting/bank-accounts       <- bank_accounts.py      (GET list)
  /api/accounting/journal-entries     <- journal_entries.py    (GET, POST batch+dedup,
                                                                PATCH, DELETE, POST /:id/reverse)
  /api/accounting/general-ledger      <- general_ledger.py     (GET)
  /api/accounting/financial-reports/* <- financial_reports.py  (GET tb/pl/bs)
  /api/accounting/invoices            <- invoices.py           (GET list/:id, POST,
                                                                POST /:id/send, POST /:id/payment)
  /api/accounting/bank-loans          <- bank_loans.py         (GET list/:id/unlinked-lines,
                                                                POST, PATCH, DELETE,
                                                                POST /:id/drawdown, /:id/payment)
  /api/accounting/disbursements       <- disbursements.py      (GET list/:id, POST, PATCH, DELETE,
                                                                POST /:id/approve /transfer
                                                                /settle /cancel)

Serialization parity (checked against the source venv, pydantic 2.13 /
fastapi 0.136): Numeric(15,2) Decimal columns go over the wire as JSON
*strings* with two decimals ("1500000.00"); the GL and financial-report money
fields are explicit float() in the source and stay JSON numbers. Datetimes
serialize as naive ISO-8601 ("2026-01-02T03:04:05"). Computed Decimal sums
that the source would render as "0" are emitted "0.00" here — feynman
parseFloat()s every money string, so only the byte form differs.

Write semantics vs the source (see each handler's docstring for detail):
ids in payloads are legacy Orion ids and resolve via orion_legacy_id first,
then ERPNext name; every response id is orion_legacy_id when present else
doc.name. GL posts through submitted ERPNext docs (JE / Sales Invoice /
Payment Entry) built with the same account+cost-center rules as
orion.migrate, so TB parity is preserved by construction.
"""

from collections import Counter
from datetime import datetime, time, timedelta
from decimal import Decimal

import frappe

from orion.accounting.loan_codes import next_doc_code, recompute_outstanding
from orion.accounting.txn_hash import compute_txn_hash, has_real_ref
from orion.compat.handle import route, split_path

JE_PREFIX = "/api/accounting/journal-entries"
FR_PREFIX = "/api/accounting/financial-reports"
INV_PREFIX = "/api/accounting/invoices"
LOAN_PREFIX = "/api/accounting/bank-loans"
DISB_PREFIX = "/api/accounting/disbursements"

# Reverse of migrate/coa.py ROOT_TYPE (many-to-one there): the account code's
# leading digit disambiguates the P&L flavors, balance-sheet roots map 1:1.
TYPE_BY_PREFIX = {
	"4": "REVENUE",
	"5": "COGS",
	"6": "EXPENSE",
	"7": "OTHER_INCOME",
	"8": "OTHER_EXPENSE",
}
TYPE_BY_ROOT = {
	"Asset": "ASSET",
	"Liability": "LIABILITY",
	"Equity": "EQUITY",
	"Income": "REVENUE",
	"Expense": "EXPENSE",
}

PL_TYPES = ("REVENUE", "COGS", "EXPENSE", "OTHER_INCOME", "OTHER_EXPENSE")
BS_TYPES = ("ASSET", "LIABILITY", "EQUITY")

# GL rows from non-JE vouchers still need a legacy sourceType label.
SOURCE_TYPE_BY_VOUCHER = {
	"Sales Invoice": "INVOICE",
	"Payment Entry": "INVOICE_PAYMENT",
}

BRAND_LINES = ("RATUNDA_RENOVASI", "POIESIS_STUDIO")

# bank_loans.py: default liability account (by code) per lender type.
DEFAULT_ACCOUNT_BY_LENDER = {
	"BANK": "2-1700",
	"OWNER": "2-1500",
	"THIRD_PARTY": "2-1800",
}
LOAN_INTEREST_CODE = "8-1100"

# Window used when looking for an already-posted JE that matches a drawdown.
DUPLICATE_WINDOW_DAYS = 3

# disbursements.py: per-category COGS account codes + the funding bank.
CATEGORY_GL = {
	"MATERIAL": "5-1100",
	"UPAH": "5-1200",
	"OPERASIONAL": "5-1600",
	"TRANSPORT": "5-1400",
}
KOPRA_GL = "1-1120"

# invoices.py: AR / default revenue account codes per business line.
AR_CODE = {"POIESIS_STUDIO": "1-1220", "RATUNDA_RENOVASI": "1-1210"}
REV_CODE = {"POIESIS_STUDIO": "4-2100", "RATUNDA_RENOVASI": "4-1100"}


@route("/api/accounting/accounts")
def accounts(path: str, verb: str, payload: dict):
	"""CoA list — app/routers/accounting/accounts.py GET '' -> {accounts: [AccountRead]}."""
	_read_only(verb, "/api/accounting/accounts")
	company = _company()
	rows = _account_rows(company)
	legacy_by_name = {r.name: (r.orion_legacy_id or r.name) for r in rows}
	bank_by_gl = _bank_meta_by_gl()

	out = []
	for r in rows:
		bank = bank_by_gl.get(r.name)
		out.append(
			{
				"code": r.account_number or "",
				"name": r.account_name,
				"type": _orion_type(r),
				"businessLine": r.orion_business_line or "ALL",
				"parentId": legacy_by_name.get(r.parent_account),
				"isHeader": bool(r.is_group),
				"isBank": r.account_type == "Bank",
				"isCash": r.account_type == "Cash",
				"bankName": bank[0] if bank else None,
				"accountNumber": bank[1] if bank else None,
				"description": None,
				"normalBalance": _normal_balance(r.root_type),
				"id": legacy_by_name[r.name],
				"isActive": not r.disabled,
				"createdAt": _iso(r.creation),
			}
		)
	return {"accounts": out}


@route("/api/accounting/bank-accounts")
def bank_accounts(path: str, verb: str, payload: dict):
	"""Bank account list — app/routers/accounting/bank_accounts.py GET ''
	-> {bankAccounts: [BankAccountRead]}, ordered createdAt asc; the embedded
	`vendor` stub is always null in the source (_to_read) too."""
	_read_only(verb, "/api/accounting/bank-accounts")
	acc_by_name = {r.name: r for r in _account_rows(_company())}
	rows = frappe.get_all(
		"Bank Account",
		fields=[
			"name", "account_name", "bank", "bank_account_no", "account",
			"is_company_account", "disabled", "party_type", "party", "creation",
			"orion_owner_type", "orion_purpose", "orion_legacy_id",
		],
		order_by="creation asc",
	)

	out = []
	for r in rows:
		gl = acc_by_name.get(r.account)
		vendor_id = None
		employee_id = None
		if r.party_type == "Supplier" and r.party:
			vendor_id = frappe.db.get_value("Supplier", r.party, "orion_legacy_id") or r.party
		elif r.party_type == "Employee" and r.party:
			employee_id = r.party
		out.append(
			{
				"bankName": r.bank,
				"accountNumber": r.bank_account_no,
				"accountHolderName": r.account_name,
				"ownerType": r.orion_owner_type
				or ("COMPANY" if r.is_company_account else "VENDOR"),
				"accountId": (gl.orion_legacy_id or gl.name) if gl else None,
				"vendorId": vendor_id,
				"employeeId": employee_id,
				"businessLine": (gl.orion_business_line if gl else None) or "ALL",
				"purpose": r.orion_purpose,
				"currency": (gl.account_currency if gl else None) or "IDR",
				"notes": None,
				"id": r.orion_legacy_id or r.name,
				"isActive": not r.disabled,
				"createdAt": _iso(r.creation),
				"account": _account_stub(gl),
				"vendor": None,
			}
		)
	return {"bankAccounts": out}


@route(JE_PREFIX)
def journal_entries(path: str, verb: str, payload: dict):
	"""Journal entries — app/routers/accounting/journal_entries.py.

	GET ''            -> {entries: [JournalEntryRead]} ordered date desc.
	GET /:id          -> {entry: JournalEntryRead} (JournalEntryEnvelope shape;
	                     the source router has no GET detail — feynman only
	                     lists — but compat resolves by orion_legacy_id first,
	                     then by Journal Entry name).
	POST ''           -> create batch (JournalEntryService.create_batch): per-
	                     entry balance check, BANK_IMPORT txn-hash dedup with
	                     within-batch collision suffixing and cross-account
	                     reference dedup; returns CreateJournalEntriesResponse
	                     {entries, created, skipped, skippedList}.
	PATCH /:id        -> MANUAL entries: full replace (cancel+delete+recreate,
	                     orion_legacy_id carried so the emitted id is stable);
	                     non-MANUAL: projectId-only reallocation (rows + GL).
	DELETE /:id       -> MANUAL only: cancel + delete -> {success: true}.
	POST /:id/reverse -> reversal JE (swapped Dr/Cr, reversal_of link) with the
	                     source's cascade: loan events deleted + loan resynced,
	                     cash advances reverted to APPROVED.
	"""
	bare, _query = split_path(path)
	rest = bare[len(JE_PREFIX):].strip("/")
	parts = rest.split("/") if rest else []
	if verb == "GET" and not parts:
		return {"entries": _je_read(_je_headers())}
	if verb == "POST" and not parts:
		return _je_create_batch(payload)
	if len(parts) == 1:
		if verb == "GET":
			return {"entry": _je_detail(parts[0])}
		if verb == "PATCH":
			return {"entry": _je_update(parts[0], payload)}
		if verb == "DELETE":
			return _je_delete(parts[0])
	if verb == "POST" and len(parts) == 2 and parts[1] == "reverse":
		return {"reversalEntry": _je_reverse(parts[0], payload)}
	frappe.throw("No compat handler for %s %s" % (verb, bare), exc=frappe.DoesNotExistError)


@route("/api/accounting/general-ledger")
def general_ledger(path: str, verb: str, payload: dict):
	"""General ledger — app/routers/accounting/general_ledger.py GET
	?accountId&from&to -> {account, openingBalance, closingBalance,
	totalDebit, totalCredit, rows: [GLRow]} (money as JSON numbers)."""
	_read_only(verb, "/api/accounting/general-ledger")
	_bare, query = split_path(path)
	account_id = query.get("accountId")
	if not account_id:
		frappe.throw("accountId is required")
	acc = _resolve_account(account_id)
	if not acc:
		frappe.throw("Account not found", exc=frappe.DoesNotExistError)

	company = _company()
	normal = _normal_balance(acc.root_type)
	from_d = _parse_date(query.get("from"))
	to_d = _parse_date(query.get("to"))

	opening = 0.0
	if from_d:
		row = frappe.db.sql(
			"""select coalesce(sum(debit), 0), coalesce(sum(credit), 0)
			from `tabGL Entry`
			where company = %s and account = %s and is_cancelled = 0
				and posting_date < %s""",
			(company, acc.name, from_d.date()),
		)[0]
		d, c = float(row[0]), float(row[1])
		opening = (d - c) if normal == "DEBIT" else (c - d)

	filters = [
		["company", "=", company],
		["account", "=", acc.name],
		["is_cancelled", "=", 0],
	]
	if from_d:
		filters.append(["posting_date", ">=", from_d.date()])
	if to_d:
		filters.append(["posting_date", "<=", to_d.date()])
	gles = frappe.get_all(
		"GL Entry",
		filters=filters,
		fields=[
			"name", "posting_date", "debit", "credit",
			"voucher_type", "voucher_no", "remarks", "creation",
		],
		order_by="posting_date asc, creation asc",
	)

	je_meta = _je_meta(
		{g.voucher_no for g in gles if g.voucher_type == "Journal Entry"}
	)

	running = opening
	total_debit = 0.0
	total_credit = 0.0
	rows = []
	for g in gles:
		d = float(g.debit or 0)
		c = float(g.credit or 0)
		running += (d - c) if normal == "DEBIT" else (c - d)
		total_debit += d
		total_credit += c

		je = je_meta.get(g.voucher_no) if g.voucher_type == "Journal Entry" else None
		line_note = g.remarks if g.remarks and g.remarks != "No Remarks" else None
		if je:
			entry_id = je.orion_legacy_id or je.name
			description = je.user_remark or line_note or ""
			reference = je.cheque_no
			source_type = je.orion_source_type or "MANUAL"
		else:
			entry_id = g.voucher_no
			description = line_note or g.voucher_no
			reference = g.voucher_no
			source_type = SOURCE_TYPE_BY_VOUCHER.get(g.voucher_type, "MANUAL")
		rows.append(
			{
				"id": g.name,
				"entryId": entry_id,
				"date": _iso_date(g.posting_date),
				"description": description,
				"reference": reference,
				"sourceType": source_type,
				"lineNote": line_note,
				"debit": d,
				"credit": c,
				"balance": running,
			}
		)

	return {
		"account": {
			"id": acc.orion_legacy_id or acc.name,
			"code": acc.account_number or "",
			"name": acc.account_name,
			"normalBalance": normal,
			"type": _orion_type(acc),
		},
		"openingBalance": opening,
		"closingBalance": running,
		"totalDebit": total_debit,
		"totalCredit": total_credit,
		"rows": rows,
	}


@route(FR_PREFIX)
def financial_reports(path: str, verb: str, payload: dict):
	"""Financial reports — app/routers/accounting/financial_reports.py
	(GET /trial-balance, /profit-loss, /balance-sheet)."""
	_read_only(verb, FR_PREFIX)
	bare, query = split_path(path)
	report = bare[len(FR_PREFIX):].strip("/")
	if report == "trial-balance":
		return _trial_balance(query)
	if report == "profit-loss":
		return _profit_loss(query)
	if report == "balance-sheet":
		return _balance_sheet(query)
	frappe.throw("No compat handler for %s" % bare, exc=frappe.DoesNotExistError)


def _trial_balance(query: dict) -> dict:
	"""financial_reports.py trial_balance — active leaf accounts sorted by
	code, per-account debit/credit sums over the period, normal-balance
	signed balance, floats throughout."""
	from_, to = query.get("from"), query.get("to")
	if not from_ or not to:
		frappe.throw("from and to are required")
	bl = query.get("businessLine") or "ALL"
	company = _company()

	accounts = [
		r
		for r in _account_rows(company)
		if not r.disabled and not r.is_group and _bl_ok(r, bl)
	]
	aggs = _aggregate(company, _parse_date(from_).date(), _parse_date(to).date())

	rows = []
	total_debit = 0.0
	total_credit = 0.0
	for a in accounts:
		d, c = aggs.get(a.name, (0.0, 0.0))
		normal = _normal_balance(a.root_type)
		balance = (d - c) if normal == "DEBIT" else (c - d)
		total_debit += d
		total_credit += c
		rows.append(
			{
				"code": a.account_number or "",
				"name": a.account_name,
				"type": _orion_type(a),
				"normalBalance": normal,
				"totalDebit": d,
				"totalCredit": c,
				"balance": balance,
			}
		)

	return {
		"period": {"from": from_, "to": to},
		"accounts": rows,
		"totalDebit": total_debit,
		"totalCredit": total_credit,
		"isBalanced": abs(total_debit - total_credit) < 0.01,
	}


def _profit_loss(query: dict) -> dict:
	"""financial_reports.py profit_loss — three buckets only, per Lastiko
	(2026-07): OTHER_INCOME folds into revenue, OTHER_EXPENSE into expenses;
	otherIncome/otherExpenses stay as empty stubs for older consumers; header
	accounts ride along in `headers` so feynman renders the CoA tree
	(FinancialReports.vue)."""
	from_, to = query.get("from"), query.get("to")
	if not from_ or not to:
		frappe.throw("from and to are required")
	bl = query.get("businessLine") or "ALL"
	company = _company()

	rows = _account_rows(company)
	legacy_by_name = {r.name: (r.orion_legacy_id or r.name) for r in rows}
	all_accounts = [
		r
		for r in rows
		if not r.disabled and _orion_type(r) in PL_TYPES and _bl_ok(r, bl)
	]
	accounts = [a for a in all_accounts if not a.is_group]
	header_accounts = [a for a in all_accounts if a.is_group]

	aggs = _aggregate(company, _parse_date(from_).date(), _parse_date(to).date())

	revenue = _build_section(accounts, aggs, legacy_by_name, ("REVENUE", "OTHER_INCOME"))
	cogs = _build_section(accounts, aggs, legacy_by_name, ("COGS",))
	gross_profit = revenue["total"] - cogs["total"]
	expenses = _build_section(accounts, aggs, legacy_by_name, ("EXPENSE", "OTHER_EXPENSE"))
	net_income = gross_profit - expenses["total"]

	return {
		"period": {"from": from_, "to": to},
		"revenue": revenue,
		"cogs": cogs,
		"grossProfit": gross_profit,
		"expenses": expenses,
		"operatingIncome": net_income,
		"otherIncome": {"accounts": [], "total": 0.0},
		"otherExpenses": {"accounts": [], "total": 0.0},
		"netIncome": net_income,
		"headers": [_account_row(a, aggs, legacy_by_name) for a in header_accounts],
	}


def _balance_sheet(query: dict) -> dict:
	"""financial_reports.py balance_sheet — cumulative balances up to asOf;
	no synthetic retained-earnings row, exactly like the source (isBalanced
	only holds once closing entries exist)."""
	as_of = query.get("asOf")
	if not as_of:
		frappe.throw("asOf is required")
	bl = query.get("businessLine") or "ALL"
	company = _company()

	rows = _account_rows(company)
	legacy_by_name = {r.name: (r.orion_legacy_id or r.name) for r in rows}
	accounts = [
		r
		for r in rows
		if not r.disabled and not r.is_group and _orion_type(r) in BS_TYPES and _bl_ok(r, bl)
	]

	aggs = _aggregate(company, None, _parse_date(as_of).date())

	assets = _build_section(accounts, aggs, legacy_by_name, ("ASSET",))
	liabilities = _build_section(accounts, aggs, legacy_by_name, ("LIABILITY",))
	equity = _build_section(accounts, aggs, legacy_by_name, ("EQUITY",))
	tot_le = liabilities["total"] + equity["total"]

	return {
		"asOf": as_of,
		"assets": assets,
		"liabilities": liabilities,
		"equity": equity,
		"totalLiabilitiesAndEquity": tot_le,
		"isBalanced": abs(assets["total"] - tot_le) < 0.01,
	}


# ── journal entry builders ──────────────────────────────────────────────────


def _je_headers(name: str | None = None, names: list | None = None):
	filters = {"company": _company(), "docstatus": ("!=", 2)}
	if name:
		filters = {"name": name}
	elif names is not None:
		filters = {"name": ("in", names)}
	fields = [
		"name", "posting_date", "user_remark", "cheque_no", "creation", "modified",
		"orion_legacy_id", "orion_source_type", "orion_txn_hash",
		"orion_bank_account_no", "orion_source_file",
	]
	if _has_reversal_of():
		fields.append("reversal_of")
	return frappe.get_all(
		"Journal Entry",
		filters=filters,
		fields=fields,
		order_by="posting_date desc, creation desc",
	)


def _je_read(entries) -> list[dict]:
	"""JournalEntryRead rows (journal_entries.py _entry_to_read): line
	debit/credit as 2dp strings, `project` stub null like the source."""
	if not entries:
		return []
	acc_by_name = {r.name: r for r in _account_rows(_company())}
	emitted = {e.name: (e.orion_legacy_id or e.name) for e in entries}

	lines_by_parent = {}
	children = frappe.get_all(
		"Journal Entry Account",
		filters={"parenttype": "Journal Entry", "parent": ("in", list(emitted))},
		fields=[
			"name", "parent", "idx", "account", "debit_in_account_currency",
			"credit_in_account_currency", "user_remark", "project", "creation",
		],
		order_by="parent asc, idx asc",
	)
	for l in children:
		lines_by_parent.setdefault(l.parent, []).append(l)

	project_names = {l.project for l in children if l.project}
	project_legacy = {}
	if project_names:
		for p in frappe.get_all(
			"Project",
			filters={"name": ("in", list(project_names))},
			fields=["name", "orion_legacy_id"],
		):
			project_legacy[p.name] = p.orion_legacy_id or p.name

	out = []
	for e in entries:
		entry_id = emitted[e.name]
		lines = lines_by_parent.get(e.name) or []
		project = next((l.project for l in lines if l.project), None)
		reversal_of = e.get("reversal_of")
		out.append(
			{
				"id": entry_id,
				"date": _iso_date(e.posting_date),
				"description": e.user_remark or "",
				"reference": e.cheque_no,
				"sourceType": e.orion_source_type or "MANUAL",
				"sourceFile": e.orion_source_file,
				"bankAccount": e.orion_bank_account_no,
				"sourceId": None,  # not carried over by migrate/journal_entries.py
				"projectId": project_legacy.get(project),
				"reversalOfId": _je_emitted_id(reversal_of, emitted) if reversal_of else None,
				"txnHash": e.orion_txn_hash,
				"createdAt": _iso(e.creation),
				"updatedAt": _iso(e.modified),
				"lines": [_je_line(l, entry_id, acc_by_name) for l in lines],
				"project": None,  # cross-module enrichment deferred in the source too
			}
		)
	return out


def _je_line(l, entry_id: str, acc_by_name) -> dict:
	acc = acc_by_name.get(l.account)
	return {
		"id": l.name,
		"journalEntryId": entry_id,
		"accountId": (acc.orion_legacy_id or acc.name) if acc else l.account,
		"debit": "%.2f" % float(l.debit_in_account_currency or 0),
		"credit": "%.2f" % float(l.credit_in_account_currency or 0),
		"description": l.user_remark,
		"createdAt": _iso(l.creation),
		"account": _account_stub(acc),
	}


def _je_detail(ident: str) -> dict:
	name = frappe.db.get_value("Journal Entry", {"orion_legacy_id": ident})
	if not name and frappe.db.exists("Journal Entry", ident):
		name = ident
	if not name:
		frappe.throw("Journal entry not found", exc=frappe.DoesNotExistError)
	entries = _je_headers(name=name)
	if not entries:
		frappe.throw("Journal entry not found", exc=frappe.DoesNotExistError)
	return _je_read(entries)[0]


def _je_emitted_id(name: str, emitted: dict) -> str:
	if name in emitted:
		return emitted[name]
	return frappe.db.get_value("Journal Entry", name, "orion_legacy_id") or name


def _je_meta(names: set) -> dict:
	if not names:
		return {}
	fields = ["name", "user_remark", "cheque_no", "orion_source_type", "orion_legacy_id"]
	return {
		r.name: r
		for r in frappe.get_all(
			"Journal Entry", filters={"name": ("in", list(names))}, fields=fields
		)
	}


def _has_reversal_of() -> bool:
	return frappe.db.has_column("Journal Entry", "reversal_of")


# ── report builders ─────────────────────────────────────────────────────────


def _aggregate(company: str, from_date, to_date) -> dict:
	"""Per-account (debit, credit) float sums over posted GL in the range —
	financial_reports.py _aggregate, over tabGL Entry instead of journal_lines."""
	conditions = "company = %s and is_cancelled = 0"
	params = [company]
	if from_date:
		conditions += " and posting_date >= %s"
		params.append(from_date)
	if to_date:
		conditions += " and posting_date <= %s"
		params.append(to_date)
	rows = frappe.db.sql(
		"""select account,
			coalesce(sum(debit), 0) as d,
			coalesce(sum(credit), 0) as c
		from `tabGL Entry` where %s group by account"""
		% conditions,
		params,
		as_dict=True,
	)
	return {r.account: (float(r.d), float(r.c)) for r in rows}


def _account_row(a, aggs: dict, legacy_by_name: dict) -> dict:
	"""financial_reports.py _account_row. The EQUITY+DEBIT flip is kept for
	parity even though compat derives normalBalance from root_type (so
	equity rows always read CREDIT and the c-d arithmetic already matches
	the source's flipped value)."""
	d, c = aggs.get(a.name, (0.0, 0.0))
	a_type = _orion_type(a)
	normal = _normal_balance(a.root_type)
	if a_type == "EQUITY" and normal == "DEBIT":
		balance = -(d - c)
	elif normal == "DEBIT":
		balance = d - c
	else:
		balance = c - d
	return {
		"id": legacy_by_name.get(a.name, a.name),
		"code": a.account_number or "",
		"name": a.account_name,
		"type": a_type,
		"normalBalance": normal,
		"parentId": legacy_by_name.get(a.parent_account),
		"isHeader": bool(a.is_group),
		"totalDebit": d,
		"totalCredit": c,
		"balance": balance,
	}


def _build_section(accounts, aggs, legacy_by_name, types) -> dict:
	section = [
		_account_row(a, aggs, legacy_by_name) for a in accounts if _orion_type(a) in types
	]
	return {"accounts": section, "total": float(sum(s["balance"] for s in section))}


# ── shared helpers ──────────────────────────────────────────────────────────


def _read_only(verb: str, endpoint: str):
	if verb != "GET":
		frappe.throw(
			"%s %s is not yet implemented in compat — Phase A is read-only"
			% (verb, endpoint)
		)


def _company() -> str:
	company = frappe.get_cached_doc("Orion Settings").company
	if not company:
		frappe.throw("Orion Settings has no company")
	return company


def _account_rows(company: str):
	"""All company accounts, sorted by code like the source list_accounts."""
	return frappe.get_all(
		"Account",
		filters={"company": company},
		fields=[
			"name", "account_name", "account_number", "root_type", "is_group",
			"account_type", "disabled", "parent_account", "account_currency",
			"orion_business_line", "orion_legacy_id", "creation",
		],
		order_by="account_number asc, name asc",
	)


def _resolve_account(ident: str):
	"""By orion_legacy_id first (grp- synthetic ids included), then by name."""
	fields = [
		"name", "account_name", "account_number", "root_type",
		"orion_legacy_id", "orion_business_line",
	]
	acc = frappe.db.get_value(
		"Account", {"orion_legacy_id": ident}, fields, as_dict=True
	)
	if not acc:
		acc = frappe.db.get_value("Account", ident, fields, as_dict=True)
	return acc


def _account_stub(acc) -> dict | None:
	if not acc:
		return None
	return {
		"id": acc.orion_legacy_id or acc.name,
		"code": acc.account_number or "",
		"name": acc.account_name,
	}


def _bank_meta_by_gl() -> dict:
	"""GL account -> (bank name, account number) for the accounts endpoint's
	bankName/accountNumber columns (kept on accounts in the legacy schema)."""
	rows = frappe.get_all(
		"Bank Account",
		filters={"account": ("is", "set")},
		fields=["account", "bank", "bank_account_no"],
		order_by="creation asc",
	)
	out = {}
	for r in rows:
		out.setdefault(r.account, (r.bank, r.bank_account_no))
	return out


def _orion_type(row) -> str:
	prefix = (row.account_number or "")[:1]
	if prefix in TYPE_BY_PREFIX:
		return TYPE_BY_PREFIX[prefix]
	return TYPE_BY_ROOT.get(row.root_type, "ASSET")


def _normal_balance(root_type: str) -> str:
	return "DEBIT" if root_type in ("Asset", "Expense") else "CREDIT"


def _bl_ok(row, business_line: str) -> bool:
	"""financial_reports.py _bl_filter: businessLine != ALL admits {ALL, bl}.
	The source filters at the *account* level (Account.businessLine), so
	compat filters on Account.orion_business_line — no cost-center pass
	needed; unset business lines count as ALL."""
	if business_line == "ALL":
		return True
	return (row.orion_business_line or "ALL") in ("ALL", business_line)


def _parse_date(s: str | None):
	if not s:
		return None
	return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def _iso(dt) -> str | None:
	if not dt:
		return None
	if isinstance(dt, str):
		dt = frappe.utils.get_datetime(dt)
	return dt.isoformat()


def _iso_date(d) -> str | None:
	"""posting_date (a date) in the source's DateTime shape: midnight ISO."""
	if not d:
		return None
	return "%sT00:00:00" % d


# ── write helpers ───────────────────────────────────────────────────────────


def _settings():
	return frappe.get_cached_doc("Orion Settings")


def _cc_by_line() -> dict:
	s = _settings()
	return {
		"RATUNDA_RENOVASI": s.ratunda_cost_center,
		"POIESIS_STUDIO": s.poiesis_cost_center,
	}


def _resolve_name(doctype: str, ident):
	"""Doc name from a legacy Orion id (orion_legacy_id) first, then an
	ERPNext name — the inverse of how every response id is emitted."""
	if not ident:
		return None
	name = frappe.db.get_value(doctype, {"orion_legacy_id": ident})
	if not name and frappe.db.exists(doctype, ident):
		name = ident
	return name


def _legacy_or_name(doctype: str, name):
	if not name:
		return None
	return frappe.db.get_value(doctype, name, "orion_legacy_id") or name


def _wdec(v) -> Decimal:
	"""Payload/db value -> Decimal exactly (None -> 0), like migrate.dec."""
	if v is None:
		return Decimal("0")
	if isinstance(v, Decimal):
		return v
	return Decimal(str(v))


def _q2(v) -> Decimal:
	return _wdec(v).quantize(Decimal("0.01"))


def _money(v):
	"""Decimal-exact 2dp value as float for Currency fields."""
	if v is None:
		return None
	return float(_q2(v))


def _s2(v) -> str:
	"""Numeric(15,2) serialization: 2dp JSON string."""
	return "%.2f" % float(v or 0)


def _s2n(v):
	return None if v is None else "%.2f" % float(v)


def _require_account(ident):
	acc = _resolve_account(ident)
	if not acc:
		frappe.throw("Account not found: %s" % ident, exc=frappe.DoesNotExistError)
	return acc


def _account_by_number(code: str):
	return frappe.db.get_value(
		"Account",
		{"company": _company(), "account_number": code, "is_group": 0},
		["name", "account_name", "account_number", "orion_legacy_id"],
		as_dict=True,
	)


def _resolve_project_or_throw(ident):
	name = _resolve_name("Project", ident)
	if not name:
		frappe.throw("Project not found: %s" % ident, exc=frappe.DoesNotExistError)
	return name


def _insert_named(doc):
	"""Insert with a preset doc.name — frappe.flags.in_import makes preset
	names stick (same mechanism orion.migrate relies on for SI numbers)."""
	prev = frappe.flags.in_import
	frappe.flags.in_import = True
	try:
		doc.insert()
	finally:
		frappe.flags.in_import = prev
	return doc


def _post_journal_entry(
	*,
	posting_date,
	description,
	reference,
	source_type,
	lines,
	project=None,
	bank_account_no=None,
	source_file=None,
	txn_hash=None,
	legacy_id=None,
	reversal_of=None,
):
	"""Insert+submit a Journal Entry with migrate/journal_entries.py's field
	and cost-center conventions. `lines` are dicts: {account (ERPNext name),
	debit, credit (Decimal), description, and optional cost_center / project /
	party_type / party overrides}. Cost center per row: account brand line ->
	brand CC, else the header project's brand CC, else Shared."""
	settings = _settings()
	cc_by_line = _cc_by_line()
	shared_cc = settings.shared_cost_center

	entry_cc = None
	if project:
		entry_cc = cc_by_line.get(
			frappe.db.get_value("Project", project, "orion_business_line")
		)

	acc_bl = {}
	for l in lines:
		if l["account"] not in acc_bl:
			acc_bl[l["account"]] = frappe.db.get_value(
				"Account", l["account"], "orion_business_line"
			)

	doc = frappe.new_doc("Journal Entry")
	doc.voucher_type = "Journal Entry"
	doc.company = settings.company
	doc.posting_date = posting_date
	if reference:
		doc.cheque_no = reference
		doc.cheque_date = posting_date
	doc.user_remark = description
	doc.multi_currency = 0
	doc.orion_source_type = source_type
	doc.orion_txn_hash = txn_hash
	doc.orion_bank_account_no = bank_account_no
	doc.orion_source_file = source_file
	doc.orion_legacy_id = legacy_id
	if reversal_of and _has_reversal_of():
		doc.reversal_of = reversal_of

	for l in lines:
		bl = acc_bl.get(l["account"])
		if l.get("cost_center"):
			cc = l["cost_center"]
		elif bl in BRAND_LINES:
			cc = cc_by_line[bl]
		elif entry_cc:
			cc = entry_cc
		else:
			cc = shared_cc
		doc.append(
			"accounts",
			{
				"account": l["account"],
				"debit_in_account_currency": _money(l.get("debit")) or 0,
				"credit_in_account_currency": _money(l.get("credit")) or 0,
				"user_remark": l.get("description"),
				"project": l["project"] if "project" in l else project,
				"cost_center": cc,
				"party_type": l.get("party_type"),
				"party": l.get("party"),
			},
		)

	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	return doc


def _assert_balanced(lines, label):
	"""JournalEntryService.assert_balanced — 0.01 tolerance, same message."""
	debit = sum((_wdec(l.get("debit")) for l in lines), Decimal("0"))
	credit = sum((_wdec(l.get("credit")) for l in lines), Decimal("0"))
	if abs(debit - credit) > Decimal("0.01"):
		frappe.throw(
			'Entry "%s" is not balanced (debit %s ≠ credit %s)' % (label, debit, credit)
		)


def _drop_journal_entry(doc):
	"""Cancel + hard-delete, like migrate/invoices.py drop_journal_entry —
	the source hard-deletes journal rows too."""
	doc.flags.ignore_permissions = True
	if doc.docstatus == 1:
		doc.cancel()
	frappe.delete_doc("Journal Entry", doc.name, force=1, ignore_permissions=True)


def _resync_loan_status(loan):
	"""Recompute an Orion Loan's status from its remaining events after one is
	removed (JE reversal). Outstanding itself is event-sourced (never stored);
	only the ACTIVE/PAID_OFF flag is persisted. Revolving loans stay ACTIVE."""
	# recompute_outstanding unpacks (direction, principal_amount) pairs — plain
	# tuples, not dicts
	rows = frappe.db.sql(
		"""SELECT direction, SUM(principal_amount)
		FROM `tabOrion Loan Event` WHERE loan = %s GROUP BY direction""",
		loan,
	)
	outstanding = recompute_outstanding(rows)
	is_revolving = frappe.db.get_value("Orion Loan", loan, "is_revolving")
	status = "ACTIVE" if is_revolving or outstanding > Decimal("0.01") else "PAID_OFF"
	frappe.db.set_value("Orion Loan", loan, "status", status, update_modified=False)


# ── journal entry writes ────────────────────────────────────────────────────


def _payload_je_lines(raw_lines) -> list[dict]:
	return [
		{
			"account": _require_account(l.get("accountId")).name,
			"debit": _wdec(l.get("debit")),
			"credit": _wdec(l.get("credit")),
			"description": l.get("description"),
		}
		for l in raw_lines
	]


def _entry_max_amount(lines) -> Decimal:
	"""Node parity: amount = max over lines of max(debit, credit)."""
	return max(
		(max(_wdec(l.get("debit")), _wdec(l.get("credit"))) for l in lines),
		default=Decimal("0"),
	)


def _amount_key(amount: Decimal) -> str:
	"""Integral amounts render without a fractional part (JS `${amount}`)."""
	if amount == amount.to_integral_value():
		return str(int(amount))
	return str(amount)


def _je_create_batch(payload: dict) -> dict:
	"""POST '' — JournalEntryService.create_batch. Per-entry balance check;
	BANK_IMPORT entries dedup on the txn hash (payload txnHash, else computed
	exactly like the source via orion.accounting.txn_hash) with the source's
	within-batch `|N` collision suffix and the cross-account reference check;
	residual unique-key collisions on insert are skipped per-row via savepoint
	(the source's per-row IntegrityError handling). Returns
	{entries, created, skipped, skippedList}."""
	entries = payload.get("entries") or []
	if not entries:
		frappe.throw("entries is required")

	for e in entries:
		if not e.get("date") or not e.get("description") or not e.get("lines"):
			frappe.throw("Each entry needs date, description, and at least one line")
		_assert_balanced(e["lines"], e["description"])

	hash_map: dict[int, str] = {}
	for idx, e in enumerate(entries):
		if (e.get("sourceType") or "MANUAL") != "BANK_IMPORT" or not e.get("bankAccount"):
			continue
		if e.get("txnHash"):
			hash_map[idx] = e["txnHash"]
		else:
			date_key = _parse_date(e["date"]).strftime("%Y-%m-%d")
			amount = _entry_max_amount(e["lines"])
			hash_map[idx] = compute_txn_hash(
				e["bankAccount"],
				date_key,
				e.get("reference") or "-",
				float(amount),
				e["description"],
			)

	# Disambiguate within-batch hash collisions (shared BCA batch references):
	# suffix the 2nd+ occurrence with |N, same input order -> same suffixes.
	hash_counts = Counter(hash_map.values())
	if any(c > 1 for c in hash_counts.values()):
		seen: dict[str, int] = {}
		for idx in sorted(hash_map):
			h = hash_map[idx]
			if hash_counts[h] > 1:
				seen[h] = seen.get(h, 0) + 1
				if seen[h] > 1:
					hash_map[idx] = "%s|%s" % (h, seen[h])

	skipped_hashes: set[str] = set()
	if hash_map:
		existing = frappe.get_all(
			"Journal Entry",
			filters={"orion_txn_hash": ("in", list(hash_map.values()))},
			pluck="orion_txn_hash",
		)
		skipped_hashes.update(h for h in existing if h)

	# Cross-account reference dedup: the same real bank reference + date +
	# amount already imported (possibly under another account's hash).
	refs = list(
		{
			e.get("reference")
			for e in entries
			if (e.get("sourceType") or "MANUAL") == "BANK_IMPORT"
			and has_real_ref(e.get("reference"))
		}
	)
	if refs:
		headers = frappe.get_all(
			"Journal Entry",
			filters={
				"cheque_no": ("in", refs),
				"orion_source_type": "BANK_IMPORT",
				"docstatus": ("!=", 2),
			},
			fields=["name", "cheque_no", "posting_date"],
		)
		lines_by_parent: dict[str, list] = {}
		if headers:
			for l in frappe.get_all(
				"Journal Entry Account",
				filters={"parent": ("in", [h.name for h in headers])},
				fields=["parent", "debit_in_account_currency", "credit_in_account_currency"],
			):
				lines_by_parent.setdefault(l.parent, []).append(l)
		existing_ref_keys = set()
		for h in headers:
			a = max(
				(
					max(_wdec(l.debit_in_account_currency), _wdec(l.credit_in_account_currency))
					for l in lines_by_parent.get(h.name, [])
				),
				default=Decimal("0"),
			)
			existing_ref_keys.add("%s|%s|%s" % (h.cheque_no, h.posting_date, _amount_key(a)))
		for idx, e in enumerate(entries):
			if not has_real_ref(e.get("reference")):
				continue
			h = hash_map.get(idx)
			if not h:
				continue
			date_key = _parse_date(e["date"]).strftime("%Y-%m-%d")
			amount = _entry_max_amount(e["lines"])
			if "%s|%s|%s" % (e["reference"], date_key, _amount_key(amount)) in existing_ref_keys:
				skipped_hashes.add(h)

	created: list[str] = []
	skipped_list: list[str] = []
	for idx, e in enumerate(entries):
		h = hash_map.get(idx)
		date_key = _parse_date(e["date"]).strftime("%Y-%m-%d")
		if h and h in skipped_hashes:
			skipped_list.append("%s — %s" % (date_key, e["description"]))
			continue
		project = _resolve_project_or_throw(e["projectId"]) if e.get("projectId") else None
		lines = _payload_je_lines(e["lines"])
		sp = "orion_je_%s" % idx
		frappe.db.savepoint(sp)
		try:
			doc = _post_journal_entry(
				posting_date=date_key,
				description=e["description"],
				reference=e.get("reference"),
				source_type=e.get("sourceType") or "MANUAL",
				lines=lines,
				project=project,
				bank_account_no=e.get("bankAccount"),
				source_file=e.get("sourceFile"),
				txn_hash=h,
			)
		except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
			frappe.db.rollback(save_point=sp)
			skipped_list.append("%s — %s" % (date_key, e["description"]))
			continue
		created.append(doc.name)

	headers = _je_headers(names=created) if created else []
	by_name = {r.name: r for r in headers}
	reads = {r["id"]: r for r in _je_read([by_name[n] for n in created if n in by_name])}
	ordered = []
	for n in created:
		r = by_name.get(n)
		if r:
			ordered.append(reads[r.orion_legacy_id or r.name])
	return {
		"entries": ordered,
		"created": len(created),
		"skipped": len(skipped_list),
		"skippedList": skipped_list,
	}


def _je_update(ident: str, payload: dict) -> dict:
	"""PATCH /:id — JournalEntryService.update. MANUAL entries are replaced
	wholesale: ERPNext forbids editing submitted vouchers in place, so the JE
	is cancelled+deleted and re-posted from the payload (net GL equals the
	source's line replacement); the old orion_legacy_id — or, for compat-born
	entries, the old name — rides along as the new orion_legacy_id so the
	entry keeps its external id. Non-MANUAL entries only accept projectId
	(feynman's allocate flow); JE rows and GL entries are restamped."""
	name = _resolve_name("Journal Entry", ident)
	if not name:
		frappe.throw("Not found", exc=frappe.DoesNotExistError)
	doc = frappe.get_doc("Journal Entry", name)
	source_type = doc.orion_source_type or "MANUAL"

	if source_type != "MANUAL":
		if "projectId" in payload:
			_je_set_project(doc, payload.get("projectId"))
			return _je_read(_je_headers(name=name))[0]
		frappe.throw(
			"Only MANUAL entries can be fully edited. "
			"Non-MANUAL entries only support projectId updates.",
			exc=frappe.PermissionError,
		)

	if not payload.get("date") or not payload.get("description") or not payload.get("lines"):
		frappe.throw("date, description, and lines are required")
	_assert_balanced(payload["lines"], doc.user_remark or payload["description"])

	project = _resolve_project_or_throw(payload["projectId"]) if payload.get("projectId") else None
	lines = _payload_je_lines(payload["lines"])
	legacy = doc.orion_legacy_id or doc.name
	bank_no = doc.orion_bank_account_no
	source_file = doc.orion_source_file
	txn_hash = doc.orion_txn_hash
	_drop_journal_entry(doc)

	new_doc = _post_journal_entry(
		posting_date=_parse_date(payload["date"]).date(),
		description=payload["description"],
		reference=payload.get("reference"),
		source_type="MANUAL",
		lines=lines,
		project=project,
		bank_account_no=bank_no,
		source_file=source_file,
		txn_hash=txn_hash,
		legacy_id=legacy,
	)
	return _je_read(_je_headers(name=new_doc.name))[0]


def _je_set_project(doc, project_ident):
	"""Non-MANUAL projectId-only update: restamp JE rows and posted GL.
	Project does not touch TB parity — only per-project reporting."""
	project = _resolve_project_or_throw(project_ident) if project_ident else None
	frappe.db.sql(
		"update `tabJournal Entry Account` set project = %s where parent = %s",
		(project, doc.name),
	)
	frappe.db.sql(
		"""update `tabGL Entry` set project = %s
		where voucher_type = 'Journal Entry' and voucher_no = %s""",
		(project, doc.name),
	)


def _je_delete(ident: str) -> dict:
	"""DELETE /:id — MANUAL only (source PermissionError message), then
	cancel + hard delete like the source's row delete."""
	name = _resolve_name("Journal Entry", ident)
	if not name:
		frappe.throw("Not found", exc=frappe.DoesNotExistError)
	doc = frappe.get_doc("Journal Entry", name)
	if (doc.orion_source_type or "MANUAL") != "MANUAL":
		frappe.throw("Only MANUAL entries can be deleted", exc=frappe.PermissionError)
	_drop_journal_entry(doc)
	return {"success": True}


def _je_reverse(ident: str, payload: dict) -> dict:
	"""POST /:id/reverse — JournalEntryService.reverse + the je_events
	cascade. The reversal copies every row with Dr/Cr swapped (same account,
	cost center, project, party) and links reversal_of. Cascades: LOAN_* JEs
	delete the Orion Loan Event backed by the entry and resync the loan;
	CASH_ADVANCE reverts the Orion Cash Advance to APPROVED. AR_INVOICE has no
	doc cascade here — invoice GL lives on Sales Invoice / Payment Entry docs
	in ERPNext, and rows referencing an SI cannot carry the reference on the
	flipped (debit) side, so the reversal restores the account balance but not
	the SI allocation; cancel the Payment Entry in ERPNext instead."""
	name = _resolve_name("Journal Entry", ident)
	if not name:
		frappe.throw("Journal entry not found", exc=frappe.DoesNotExistError)
	doc = frappe.get_doc("Journal Entry", name)
	if _has_reversal_of():
		if doc.get("reversal_of"):
			frappe.throw("Tidak dapat membatalkan entri reversal")
		if frappe.db.exists("Journal Entry", {"reversal_of": doc.name, "docstatus": ("!=", 2)}):
			frappe.throw("Entri jurnal ini sudah pernah dibatalkan")

	rev_dt = _parse_date(payload.get("date"))
	rev_date = rev_dt.date() if rev_dt else frappe.utils.nowdate()
	reason = payload.get("reason") or "Pembatalan: %s" % (doc.user_remark or "")

	lines = []
	for row in doc.accounts:
		lines.append(
			{
				"account": row.account,
				"debit": _wdec(row.credit_in_account_currency),  # swap
				"credit": _wdec(row.debit_in_account_currency),  # swap
				"description": row.user_remark,
				"cost_center": row.cost_center,
				"project": row.project,
				"party_type": row.party_type,
				"party": row.party,
			}
		)
	reversal = _post_journal_entry(
		posting_date=rev_date,
		description=reason,
		reference=doc.cheque_no,
		source_type=doc.orion_source_type,
		lines=lines,
		reversal_of=doc.name,
	)

	if doc.orion_source_type in ("LOAN_PAYMENT", "LOAN_DRAWDOWN"):
		event = frappe.db.get_value(
			"Orion Loan Event", {"journal_entry": doc.name}, ["name", "loan"], as_dict=True
		)
		if event:
			frappe.delete_doc("Orion Loan Event", event.name, force=1, ignore_permissions=True)
			_resync_loan_status(event.loan)
	elif doc.orion_source_type == "CASH_ADVANCE":
		ca = frappe.db.get_value("Orion Cash Advance", {"journal_entry": doc.name})
		if ca:
			frappe.db.set_value(
				"Orion Cash Advance",
				ca,
				{"status": "APPROVED", "transfer_date": None, "journal_entry": None},
			)

	return _je_read(_je_headers(name=reversal.name))[0]
