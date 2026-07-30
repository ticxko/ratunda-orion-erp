"""Accounting compat handlers — Phase A (read-only tranche).

Mirrors the bellatrix-python routers under app/routers/accounting/ so the
feynman accounting screens work unchanged against ERPNext data:

  /api/accounting/accounts            <- accounts.py           (GET list)
  /api/accounting/bank-accounts       <- bank_accounts.py      (GET list)
  /api/accounting/journal-entries     <- journal_entries.py    (GET list, GET /:id)
  /api/accounting/general-ledger      <- general_ledger.py     (GET)
  /api/accounting/financial-reports/* <- financial_reports.py  (GET tb/pl/bs)

Serialization parity (checked against the source venv, pydantic 2.13 /
fastapi 0.136): journal line debit/credit are Numeric(15,2) Decimals and go
over the wire as JSON *strings* with two decimals ("1500000.00"); the GL and
financial-report money fields are explicit float() in the source and stay
JSON numbers. Datetimes serialize as naive ISO-8601 ("2026-01-02T03:04:05").

Write verbs (POST/PATCH/DELETE, /:id/reverse) intentionally throw — Phase A
is read-first; feynman keeps partial data in view on those failures.
"""

from datetime import datetime

import frappe

from orion.compat.handle import route, split_path

JE_PREFIX = "/api/accounting/journal-entries"
FR_PREFIX = "/api/accounting/financial-reports"

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

	GET ''    -> {entries: [JournalEntryRead]} ordered date desc.
	GET /:id  -> {entry: JournalEntryRead} (JournalEntryEnvelope shape; the
	source router has no GET detail — feynman only lists — but compat resolves
	by orion_legacy_id first, then by Journal Entry name).
	POST / PATCH / DELETE / :id/reverse -> throw (read-only tranche).
	"""
	bare, _query = split_path(path)
	rest = bare[len(JE_PREFIX):].strip("/")
	if verb == "GET" and not rest:
		return {"entries": _je_read(_je_headers())}
	if verb == "GET" and "/" not in rest:
		return {"entry": _je_detail(rest)}
	frappe.throw(
		"%s %s is not yet implemented in compat — Phase A is read-only" % (verb, bare)
	)


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


def _je_headers(name: str | None = None):
	filters = {"company": _company(), "docstatus": ("!=", 2)}
	if name:
		filters = {"name": name}
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
