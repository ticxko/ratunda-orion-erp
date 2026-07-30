"""Accounting compat handlers — Phase A tranche 2 (termins + financial rollups).

Mirrors the bellatrix-python routers so the feynman screens work unchanged:

  /api/accounting/termins            <- termins.py            (GET '', GET /heatmap)
  /api/accounting/project-financial  <- project_financial.py  (GET '', GET /:id/financials)
  /api/accounting/program-financial  <- program_financial.py  (GET '', GET /:id/financials)
  /api/accounting/voucher-types      <- voucher_types.py      (GET '' — empty, see handler)
  /api/accounting/internal-vouchers  <- internal_vouchers.py  (GET '' — empty, see handler)

Semantics ported from the source routers (verified line by line):

* Termin status derives from the linked invoice's *effective* status
  (OVERDUE when SENT past due). Decimal fields (plannedAmount, percentage,
  invoice stub money) serialize as 2dp strings — pydantic 2 renders
  Numeric(15,2) that way, same as JE lines in compat/accounting.py.
* project-financial nets debit−credit per line (_net_line) so reversals
  cancel; revenue counts REVENUE accounts only (OTHER_INCOME is NOT revenue
  there), costs count COGS/EXPENSE/OTHER_EXPENSE bucketed by account code
  via _category_for_code (ported verbatim: 5-11/5-28 material, 5-12/5-29
  labor, 5-13/5-22 subcontractor, 5-16/5-25 operational, 5-17/5-26/5-27
  commission).
* CASH_ADVANCE exclusion — the source DOES exclude the cost deltas of JEs
  whose sourceType == CASH_ADVANCE and sourceId is a TRANSFERRED/SETTLED
  disbursement of the same project (those costs enter via the disbursement
  totals/items instead); revenue deltas of such JEs still count. sourceId
  did not survive migration, so compat resolves the same JE set through
  Orion Cash Advance.journal_entry.
* AR_INVOICE-sourced GL is excluded from the JE roll-ups because invoice
  totals and payments are counted from the invoices themselves. Migration
  replaced those JEs with Sales Invoices + Payment Entries (+ reclass JEs
  that keep orion_source_type = AR_INVOICE), so compat skips GL rows from
  Sales Invoice / Payment Entry vouchers and from Journal Entries whose
  orion_source_type is AR_INVOICE; any other voucher type counts as JE GL.
* "collected" invoice money = grand_total − outstanding_amount (allocated
  Payment Entries + reclass-JE allocations — the migrated form of
  invoice_payments). Draft/cancelled invoices count as unpaid.
* kontrak value = Project.estimated_costing (legacy budget) when non-zero,
  else invoice totals + JE revenue. Margins use the source's
  round(x * 1000) / 10 arithmetic, floats throughout.
* Unset Percent fields (target_margin_pct, termin percentage) migrate as 0;
  compat emits null for falsy values, matching the source's nullable columns.

Write verbs throw (read-only tranche), like compat/accounting.py.
"""

import frappe

from orion.compat.accounting import (
	_account_rows,
	_company,
	_iso,
	_iso_date,
	_orion_type,
	_read_only,
)
from orion.compat.handle import route, split_path

TERMIN_PREFIX = "/api/accounting/termins"
PF_PREFIX = "/api/accounting/project-financial"
PG_PREFIX = "/api/accounting/program-financial"
VT_PREFIX = "/api/accounting/voucher-types"
IV_PREFIX = "/api/accounting/internal-vouchers"

EXPENSE_TYPES = ("COGS", "EXPENSE", "OTHER_EXPENSE")
COST_KEYS = ("material", "labor", "subcontractor", "operational", "commission", "transport", "other")
CA_STATUSES = ("TRANSFERRED", "SETTLED")

# Reverse of migrate/projects.py STATUS_MAP — legacy enum back out.
LEGACY_STATUS = {
	"Open": "ACTIVE",
	"Completed": "COMPLETED",
	"On hold": "ON_HOLD",
	"Cancelled": "CANCELLED",
}

# termins.py _to_read status_map (effective invoice status -> termin status).
TERMIN_STATUS = {
	"PAID": "PAID",
	"PARTIAL": "PARTIAL",
	"SENT": "SENT",
	"OVERDUE": "SENT",
	"DRAFT": "DRAFT",
	"CANCELLED": "CANCELLED",
}

PROJECT_FIELDS = [
	"name", "project_name", "status", "expected_start_date", "expected_end_date",
	"estimated_costing", "customer", "orion_business_line", "lctr", "orion_program",
	"target_margin_pct", "client_phone", "client_address", "orion_legacy_id",
]


def _category_for_code(code: str | None) -> str:
	"""project_financial.py _category_for_code, verbatim.
	Ratunda accounts are 5-1x, Poiesis mirror accounts are 5-2x."""
	if not code:
		return "other"
	if code.startswith("5-11") or code.startswith("5-28"):
		return "material"
	if code.startswith("5-12") or code.startswith("5-29"):
		return "labor"
	if code.startswith("5-13") or code.startswith("5-22"):
		return "subcontractor"
	if code.startswith("5-16") or code.startswith("5-25"):
		return "operational"
	if code.startswith("5-17") or code.startswith("5-26") or code.startswith("5-27"):
		return "commission"
	return "other"


# ── termins ─────────────────────────────────────────────────────────────────


@route(TERMIN_PREFIX)
def termins(path: str, verb: str, payload: dict):
	"""app/routers/accounting/termins.py.

	GET ''        ?projectId= -> {termins: [TerminRead]} ordered project, seq.
	GET /heatmap  ?businessLine= -> {projects: [HeatmapProject], maxTermins}.
	POST / PATCH / DELETE -> throw (read-only tranche).
	"""
	bare, query = split_path(path)
	rest = bare[len(TERMIN_PREFIX):].strip("/")
	if verb == "GET" and rest == "heatmap":
		return _termin_heatmap(query.get("businessLine"))
	if verb == "GET" and not rest:
		return {"termins": _termin_reads(query.get("projectId"))}
	if verb == "GET":
		frappe.throw("No compat handler for %s" % bare, exc=frappe.DoesNotExistError)
	frappe.throw(
		"%s %s is not yet implemented in compat — Phase A is read-only" % (verb, bare)
	)


def _termin_rows(project: str | None = None):
	filters = {"project": project} if project else {}
	return frappe.get_all(
		"Orion Project Termin",
		filters=filters,
		fields=[
			"name", "project", "seq", "label", "percentage", "planned_amount",
			"expected_date", "sales_invoice", "notes", "creation", "modified",
			"orion_legacy_id",
		],
		order_by="project asc, seq asc",
	)


def _termin_reads(project_ident: str | None) -> list[dict]:
	project = _resolve_project_name(project_ident) if project_ident else None
	if project_ident and not project:
		return []  # unknown projectId filter -> empty list, like the source
	rows = _termin_rows(project)
	project_ids = _project_legacy_map({t.project for t in rows})
	si_map = _si_map({t.sales_invoice for t in rows if t.sales_invoice})
	return [_termin_read(t, project_ids, si_map) for t in rows]


def _termin_read(t, project_ids: dict, si_map: dict) -> dict:
	out = {
		"id": t.orion_legacy_id or t.name,
		"projectId": project_ids.get(t.project, t.project),
		"seq": t.seq,
		"label": t.label or "",
		"percentage": ("%.2f" % t.percentage) if t.percentage else None,
		"plannedAmount": "%.2f" % float(t.planned_amount or 0),
		"expectedDate": _iso_date(t.expected_date),
		"invoiceId": None,
		"notes": t.notes,
		"createdAt": _iso(t.creation),
		"updatedAt": _iso(t.modified),
		"status": "UPCOMING",
		"invoice": None,
	}
	si = si_map.get(t.sales_invoice) if t.sales_invoice else None
	if not si:
		return out

	total, paid, outstanding, base = _si_amounts(si)
	effective = base
	if base == "SENT" and si.due_date and str(si.due_date) <= frappe.utils.nowdate():
		# source: inv.dueDate (midnight) < utcnow() — true any time on the due day
		effective = "OVERDUE"

	out["invoiceId"] = si.orion_legacy_id or si.name
	out["status"] = TERMIN_STATUS.get(effective, "UPCOMING")
	out["invoice"] = {
		"id": si.orion_legacy_id or si.name,
		"number": si.name,  # migrate/invoices.py keeps the legacy number as doc.name
		"effectiveStatus": effective,
		"totalAmount": "%.2f" % total,
		"paidAmount": "%.2f" % paid,
		"remaining": "%.2f" % outstanding,
		"issueDate": _iso_date(si.posting_date),
		"dueDate": _iso_date(si.due_date),
	}
	return out


def _termin_heatmap(business_line: str | None) -> dict:
	rows = _termin_rows()
	by_project = {}
	for t in rows:
		by_project.setdefault(t.project, []).append(t)

	meta = {
		r.name: r
		for r in frappe.get_all(
			"Project",
			filters={"name": ("in", list(by_project))},
			fields=["name", "project_name", "orion_business_line", "customer", "orion_legacy_id"],
		)
	} if by_project else {}
	clients = _customer_names({p.customer for p in meta.values() if p.customer})
	project_ids = _project_legacy_map(set(by_project))
	si_map = _si_map({t.sales_invoice for t in rows if t.sales_invoice})

	projects = []
	max_termins = 0
	for pid, group in by_project.items():
		p = meta.get(pid)
		bl = p.orion_business_line if p else None
		if business_line and bl != business_line:
			continue
		max_termins = max(max_termins, len(group))
		projects.append(
			{
				"id": project_ids.get(pid, pid),
				"code": pid if p else None,  # doc.name IS the legacy KN code
				"name": p.project_name if p else None,
				"businessLine": bl,
				"clientName": clients.get(p.customer) if p else None,
				"termins": [_termin_read(t, project_ids, si_map) for t in group],
			}
		)

	projects.sort(key=lambda hp: (hp["code"] or "", hp["name"] or ""))
	return {"projects": projects, "maxTermins": max_termins}


# ── project financial ───────────────────────────────────────────────────────


@route(PF_PREFIX)
def project_financial(path: str, verb: str, payload: dict):
	"""app/routers/accounting/project_financial.py.

	GET ''                 -> {projects: [summary]} (all projects, creation desc).
	GET /:id/financials    -> full per-project rollup with invoice /
	disbursement / journal-entry breakdowns.
	"""
	_read_only(verb, PF_PREFIX)
	bare, _query = split_path(path)
	rest = bare[len(PF_PREFIX):].strip("/")
	if not rest:
		return {"projects": _project_summaries(_project_rows())}
	parts = rest.split("/")
	if len(parts) == 2 and parts[1] == "financials":
		return _project_detail(parts[0])
	frappe.throw("No compat handler for %s" % bare, exc=frappe.DoesNotExistError)


def _project_rows():
	"""All company projects — the source's list_projects(None), createdAt desc."""
	return frappe.get_all(
		"Project",
		filters={"company": _company()},
		fields=PROJECT_FIELDS,
		order_by="creation desc",
	)


def _project_summaries(rows) -> list[dict]:
	"""compute_project_summaries — shared by project-financial list and
	program-financial, so both compute identical figures."""
	if not rows:
		return []
	company = _company()
	names = [p.name for p in rows]
	program_ids = _program_legacy_map()
	clients = _customer_names({p.customer for p in rows if p.customer})

	sis = frappe.get_all(
		"Sales Invoice",
		filters={"project": ("in", names), "docstatus": 1},
		fields=["name", "project", "grand_total", "outstanding_amount"],
	)
	si_by_p = {}
	for i in sis:
		si_by_p.setdefault(i.project, []).append(i)

	cas = [c for c in _ca_rows(names) if c.status in CA_STATUSES]
	ca_by_p = {}
	ca_jes_by_p = {}
	for c in cas:
		ca_by_p.setdefault(c.project, []).append(c)
		if c.journal_entry:
			ca_jes_by_p.setdefault(c.project, set()).add(c.journal_entry)

	gl = _gl_rollup(company, names, ca_jes_by_p)

	out = []
	for p in rows:
		invs = si_by_p.get(p.name, [])
		disbs = ca_by_p.get(p.name, [])
		fin = gl.get(p.name) or {"revenue": 0.0, "costs": {}}

		invoice_kontrak = sum(float(i.grand_total or 0) for i in invs)
		invoice_collected = sum(
			float(i.grand_total or 0) - float(i.outstanding_amount or 0) for i in invs
		)
		je_revenue = fin["revenue"]
		je_costs = sum(fin["costs"].values())

		budget = float(p.estimated_costing or 0)
		kontrak_value = budget if budget else (invoice_kontrak + je_revenue)
		collected = invoice_collected + je_revenue
		disbursement_costs = sum(float(d.total_amount or 0) for d in disbs)
		total_costs = disbursement_costs + je_costs
		gross_profit = collected - total_costs
		margin_base = kontrak_value if kontrak_value > 0 else collected
		gross_margin_pct = (
			float(round((margin_base - total_costs) / margin_base * 1000) / 10)
			if margin_base > 0
			else None
		)

		out.append(
			{
				"id": p.orion_legacy_id or p.name,
				"code": p.name,
				"name": p.project_name,
				"clientName": clients.get(p.customer),
				"clientPhone": p.client_phone,
				"address": p.client_address,
				"businessLine": p.orion_business_line,
				"status": LEGACY_STATUS.get(p.status, "ACTIVE"),
				"startDate": _iso_date(p.expected_start_date),
				"endDate": _iso_date(p.expected_end_date),
				"budget": budget if budget else None,
				"lctr": p.lctr,
				"programId": program_ids.get(p.orion_program) if p.orion_program else None,
				"kontrakValue": float(kontrak_value),
				"collectedAmount": float(collected),
				"totalCosts": float(total_costs),
				"grossProfit": float(gross_profit),
				"grossMarginPct": gross_margin_pct,
				"targetMarginPct": float(p.target_margin_pct) if p.target_margin_pct else None,
				"_count": {
					"purchaseOrders": 0,  # SPKs not yet migrated (source Phase 8 gap too)
					"invoices": len(invs),
					"disbursements": len(disbs),
				},
			}
		)
	return out


def _project_detail(ident: str) -> dict:
	p = frappe.db.get_value("Project", {"orion_legacy_id": ident}, PROJECT_FIELDS, as_dict=True)
	if not p and frappe.db.exists("Project", ident):
		p = frappe.db.get_value("Project", ident, PROJECT_FIELDS, as_dict=True)
	if not p:
		frappe.throw("Project not found", exc=frappe.DoesNotExistError)
	company = _company()
	clients = _customer_names({p.customer} if p.customer else set())

	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"project": p.name, "docstatus": 1},
		fields=[
			"name", "grand_total", "outstanding_amount", "docstatus",
			"posting_date", "due_date", "orion_legacy_id",
		],
		order_by="posting_date asc, creation asc",
	)
	si_names = [i.name for i in invoices]
	items_by_si = _si_items(si_names)
	payments_by_si = _si_payments(si_names)

	disbursements = [c for c in _ca_rows([p.name]) if c.status in CA_STATUSES]
	ca_jes = {c.journal_entry for c in disbursements if c.journal_entry}

	fin = _gl_rollup(company, [p.name], {p.name: ca_jes}).get(p.name) or {
		"revenue": 0.0,
		"costs": {},
	}

	invoice_kontrak = sum(float(i.grand_total or 0) for i in invoices)
	invoice_collected = sum(
		float(i.grand_total or 0) - float(i.outstanding_amount or 0) for i in invoices
	)
	je_revenue = fin["revenue"]

	contract_budget = float(p.estimated_costing or 0)
	kontrak_value = contract_budget if contract_budget else (invoice_kontrak + je_revenue)
	collected = invoice_collected + je_revenue
	outstanding_ar = kontrak_value - collected

	# disbursement items feed material/labor/operational/transport, then the
	# non-CA, non-AR_INVOICE journal GL adds the code-bucketed rest — exactly
	# the source's two-pass cost build.
	costs = {k: 0.0 for k in COST_KEYS}
	for d in disbursements:
		for item in d["_items"]:
			amt = float(item.amount or 0)
			cat = item.category
			if cat == "MATERIAL":
				costs["material"] += amt
			elif cat == "UPAH":
				costs["labor"] += amt
			elif cat == "OPERASIONAL":
				costs["operational"] += amt
			elif cat == "TRANSPORT":
				costs["transport"] += amt
	for cat, amt in fin["costs"].items():
		costs[cat] += amt
	total_costs = sum(costs.values())

	committed_po = 0.0  # cross-module purchase orders: deferred in the source too

	budget = contract_budget if contract_budget else None
	gross_profit = collected - total_costs
	gross_margin_pct = (
		float(round(gross_profit / collected * 1000) / 10) if collected > 0 else None
	)
	budget_used_pct = (
		float(round(total_costs / budget * 1000) / 10) if budget and budget > 0 else None
	)
	budget_remaining = (budget - total_costs) if budget else None

	return {
		"project": {
			"id": p.orion_legacy_id or p.name,
			"code": p.name,
			"name": p.project_name,
			"clientName": clients.get(p.customer),
			"clientPhone": p.client_phone,
			"address": p.client_address,
			"businessLine": p.orion_business_line,
			"status": LEGACY_STATUS.get(p.status, "ACTIVE"),
			"startDate": _iso_date(p.expected_start_date),
			"endDate": _iso_date(p.expected_end_date),
			"budget": budget,
		},
		"kontrakValue": float(kontrak_value),
		"jeRevenue": float(je_revenue),
		"collectedAmount": float(collected),
		"outstandingAR": float(outstanding_ar),
		"costs": {
			"material": costs["material"],
			"labor": costs["labor"],
			"subcontractor": costs["subcontractor"],
			"operational": costs["operational"],
			"commission": costs["commission"],
			"transport": costs["transport"],
			"other": costs["other"],
			"total": float(total_costs),
		},
		"committedPO": committed_po,
		"grossProfit": float(gross_profit),
		"grossMarginPct": gross_margin_pct,
		"budgetUsedPct": budget_used_pct,
		"budgetRemaining": float(budget_remaining) if budget_remaining is not None else None,
		"invoices": [
			{
				"id": i.orion_legacy_id or i.name,
				"number": i.name,
				"issueDate": _iso_date(i.posting_date),
				"dueDate": _iso_date(i.due_date),
				"status": _si_amounts(i)[3],
				"totalAmount": float(i.grand_total or 0),
				"items": items_by_si.get(i.name, []),
				"payments": payments_by_si.get(i.name, []),
			}
			for i in invoices
		],
		"disbursements": [_disbursement_read(d) for d in disbursements],
		"purchaseOrders": [],
		"journalEntries": _project_journal_entries(company, p.name),
	}


def _disbursement_read(d) -> dict:
	bank = d["_bank"]
	return {
		"id": d.orion_legacy_id or d.name,
		"code": d.code,
		"title": d.title,
		"status": d.status,
		"totalAmount": float(d.total_amount or 0),
		"transferDate": _iso_date(d.transfer_date),
		"items": [
			{
				"category": it.category,
				"item": it.item,
				"amount": float(it.amount or 0),
			}
			for it in d["_items"]
		],
		"targetAccount": (
			{
				"id": bank.orion_legacy_id or bank.name,
				"bankName": bank.bank,
				"accountNumber": bank.bank_account_no,
				"accountHolderName": bank.account_name,
			}
			if bank
			else None
		),
	}


def _project_journal_entries(company: str, project: str) -> list[dict]:
	"""All submitted JEs with a line on this project (Orion stamped the header
	project on every line, so line-level membership == the legacy header
	projectId). Includes AR_INVOICE reclass JEs, like the source includes
	AR_INVOICE journal entries in this array; the SI/PE vouchers that replaced
	the other AR_INVOICE JEs surface in `invoices`/`payments` instead."""
	parents = {
		r.parent
		for r in frappe.get_all(
			"Journal Entry Account",
			filters={"project": project, "parenttype": "Journal Entry"},
			fields=["parent"],
		)
	}
	if not parents:
		return []
	headers = frappe.get_all(
		"Journal Entry",
		filters={"name": ("in", list(parents)), "docstatus": 1},
		fields=[
			"name", "posting_date", "user_remark", "cheque_no",
			"orion_source_type", "orion_legacy_id",
		],
		order_by="posting_date asc, creation asc",
	)
	lines = frappe.get_all(
		"Journal Entry Account",
		filters={"parent": ("in", [h.name for h in headers]), "parenttype": "Journal Entry"},
		fields=[
			"name", "parent", "idx", "account", "debit_in_account_currency",
			"credit_in_account_currency", "user_remark",
		],
		order_by="parent asc, idx asc",
	)
	by_parent = {}
	for l in lines:
		by_parent.setdefault(l.parent, []).append(l)
	acc_by_name = {r.name: r for r in _account_rows(company)}

	out = []
	for h in headers:
		out.append(
			{
				"id": h.orion_legacy_id or h.name,
				"date": _iso_date(h.posting_date),
				"description": h.user_remark or "",
				"reference": h.cheque_no,
				"sourceType": h.orion_source_type or "MANUAL",
				"sourceId": None,  # not carried over by migrate/journal_entries.py
				"lines": [
					{
						"id": l.name,
						"debit": float(l.debit_in_account_currency or 0),
						"credit": float(l.credit_in_account_currency or 0),
						"description": l.user_remark,
						"account": _account_type_stub(acc_by_name.get(l.account)),
					}
					for l in by_parent.get(h.name, [])
				],
			}
		)
	return out


# ── program financial ───────────────────────────────────────────────────────


@route(PG_PREFIX)
def program_financial(path: str, verb: str, payload: dict):
	"""app/routers/accounting/program_financial.py.

	GET ''              -> {programs: [program row]}.
	GET /:id/financials -> one program row (accepts the legacy program id or
	the Orion Program docname/token).
	"""
	_read_only(verb, PG_PREFIX)
	bare, _query = split_path(path)
	rest = bare[len(PG_PREFIX):].strip("/")
	if not rest:
		return {"programs": list(_programs().values())}
	parts = rest.split("/")
	if len(parts) == 2 and parts[1] == "financials":
		programs = _programs()
		row = programs.get(parts[0])
		if row is None:
			legacy = frappe.db.get_value("Orion Program", parts[0], "orion_legacy_id")
			row = programs.get(legacy) if legacy else None
		if row is None:
			frappe.throw("Program not found", exc=frappe.DoesNotExistError)
		return row
	frappe.throw("No compat handler for %s" % bare, exc=frappe.DoesNotExistError)


def _programs() -> dict:
	"""_build_programs — group linked projects by programId, roll each up."""
	linked = [p for p in _project_rows() if p.orion_program]
	summaries = _project_summaries(linked)
	by_program = {}
	for s in summaries:
		by_program.setdefault(s["programId"], []).append(s)
	return {pid: _program_row(pid, kids) for pid, kids in by_program.items()}


def _program_row(program_id: str, children: list[dict]) -> dict:
	design = next((c for c in children if c["businessLine"] == "POIESIS_STUDIO"), None)
	build = next((c for c in children if c["businessLine"] == "RATUNDA_RENOVASI"), None)
	# Both children share the engagement token (lctr) and client.
	src = build or design or {}
	token = src.get("lctr")
	client_name = src.get("clientName")
	return {
		"programId": program_id,
		"token": token,
		"name": "%s (%s)" % (client_name, token) if client_name and token else token,
		"clientName": client_name,
		"design": design,
		"build": build,
		"blended": _blend(design, build),
	}


def _blend(design: dict | None, build: dict | None) -> dict:
	"""Element-wise sum of the two children; margin recomputed from the base."""
	children = [c for c in (design, build) if c]
	kontrak = sum(c["kontrakValue"] for c in children)
	collected = sum(c["collectedAmount"] for c in children)
	total_costs = sum(c["totalCosts"] for c in children)
	gross_profit = sum(c["grossProfit"] for c in children)
	margin_base = kontrak if kontrak > 0 else collected
	gross_margin_pct = (
		round((margin_base - total_costs) / margin_base * 1000) / 10
		if margin_base > 0
		else None
	)
	return {
		"kontrakValue": kontrak,
		"collectedAmount": collected,
		"totalCosts": total_costs,
		"grossProfit": gross_profit,
		"grossMarginPct": gross_margin_pct,
	}


# ── voucher types / internal vouchers ───────────────────────────────────────
# The legacy voucher_types and internal_vouchers tables are EMPTY in this
# deployment, and the "Orion Voucher Type" / "Orion Internal Voucher" doctypes
# (plan §2 row 65) do not exist in the app yet — they arrive with the first
# real data. Until then these return the source-shaped empty collections
# (both source endpoints respond with a bare JSON array).


@route(VT_PREFIX)
def voucher_types(path: str, verb: str, payload: dict):
	"""app/routers/accounting/voucher_types.py GET '' -> [VoucherTypeRead].
	Source: active voucher types ordered by code — table empty here."""
	_read_only(verb, VT_PREFIX)
	return []


@route(IV_PREFIX)
def internal_vouchers(path: str, verb: str, payload: dict):
	"""app/routers/accounting/internal_vouchers.py GET '' -> [InternalVoucherRead].
	Source list (with from/to/expenseAccountId/voucherTypeCode filters) over an
	empty table; every GET /:id (and files/pdf subpaths) is a 404 until the
	Orion Internal Voucher doctype lands with real data."""
	bare, _query = split_path(path)
	rest = bare[len(IV_PREFIX):].strip("/")
	if verb == "GET" and not rest:
		return []
	if verb == "GET":
		frappe.throw("Not found", exc=frappe.DoesNotExistError)
	frappe.throw(
		"%s %s is not yet implemented in compat — Phase A is read-only" % (verb, bare)
	)


# ── rollup core ─────────────────────────────────────────────────────────────


def _gl_rollup(company: str, project_names: list, ca_jes_by_project: dict) -> dict:
	"""Per-project (revenue, costs-by-category) over posted GL, netting
	debit−credit per the source's _net_line so reversals cancel.

	Skips Sales Invoice / Payment Entry vouchers and AR_INVOICE-sourced JEs
	(the migrated form of the source's `sourceType != AR_INVOICE` filter), and
	skips the *cost* deltas of JEs in ca_jes_by_project[project] (the source's
	CASH_ADVANCE + sourceId-in-disbursements exclusion). Revenue counts only
	REVENUE-typed accounts, costs only COGS/EXPENSE/OTHER_EXPENSE."""
	if not project_names:
		return {}
	rows = frappe.db.sql(
		"""select project, account, voucher_type, voucher_no,
			coalesce(sum(debit), 0) as d, coalesce(sum(credit), 0) as c
		from `tabGL Entry`
		where company = %(company)s and is_cancelled = 0
			and project in %(projects)s
		group by project, account, voucher_type, voucher_no""",
		{"company": company, "projects": tuple(project_names)},
		as_dict=True,
	)

	je_names = {r.voucher_no for r in rows if r.voucher_type == "Journal Entry"}
	src_type = {}
	if je_names:
		src_type = {
			r.name: r.orion_source_type
			for r in frappe.get_all(
				"Journal Entry",
				filters={"name": ("in", list(je_names))},
				fields=["name", "orion_source_type"],
			)
		}
	acc_by_name = {r.name: r for r in _account_rows(company)}

	out = {}
	for r in rows:
		if r.voucher_type in ("Sales Invoice", "Payment Entry"):
			continue
		if r.voucher_type == "Journal Entry" and src_type.get(r.voucher_no) == "AR_INVOICE":
			continue
		acc = acc_by_name.get(r.account)
		if not acc:
			continue
		a_type = _orion_type(acc)
		if a_type != "REVENUE" and a_type not in EXPENSE_TYPES:
			continue
		fin = out.setdefault(
			r.project, {"revenue": 0.0, "costs": {k: 0.0 for k in COST_KEYS}}
		)
		d = float(r.d or 0)
		c = float(r.c or 0)
		if a_type == "REVENUE":
			fin["revenue"] += c - d
		elif r.voucher_no not in (ca_jes_by_project.get(r.project) or ()):
			fin["costs"][_category_for_code(acc.account_number)] += d - c
	return out


def _ca_rows(project_names: list) -> list:
	"""Orion Cash Advance rows (all statuses) with child items and target bank
	account attached — callers filter by CA_STATUSES; creation asc mirrors the
	source's Disbursement.createdAt ordering."""
	if not project_names:
		return []
	rows = frappe.get_all(
		"Orion Cash Advance",
		filters={"project": ("in", list(project_names))},
		fields=[
			"name", "code", "project", "title", "status", "total_amount",
			"transfer_date", "target_bank_account", "journal_entry", "orion_legacy_id",
		],
		order_by="creation asc",
	)
	items_by_parent = {}
	if rows:
		for it in frappe.get_all(
			"Orion Cash Advance Item",
			filters={"parent": ("in", [r.name for r in rows]), "parenttype": "Orion Cash Advance"},
			fields=["parent", "idx", "category", "item", "amount"],
			order_by="parent asc, idx asc",
		):
			items_by_parent.setdefault(it.parent, []).append(it)
	bank_names = {r.target_bank_account for r in rows if r.target_bank_account}
	banks = {}
	if bank_names:
		banks = {
			b.name: b
			for b in frappe.get_all(
				"Bank Account",
				filters={"name": ("in", list(bank_names))},
				fields=["name", "bank", "bank_account_no", "account_name", "orion_legacy_id"],
			)
		}
	for r in rows:
		r["_items"] = items_by_parent.get(r.name, [])
		r["_bank"] = banks.get(r.target_bank_account)
	return rows


def _si_items(si_names: list) -> dict:
	if not si_names:
		return {}
	out = {}
	for it in frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": ("in", si_names), "parenttype": "Sales Invoice"},
		fields=["parent", "idx", "description", "item_name", "amount", "orion_percentage"],
		order_by="parent asc, idx asc",
	):
		out.setdefault(it.parent, []).append(
			{
				"description": it.description or it.item_name,
				"amount": float(it.amount or 0),
				"percentage": float(it.orion_percentage) if it.orion_percentage else None,
			}
		)
	return out


def _si_payments(si_names: list) -> dict:
	"""Migrated invoice_payments: Payment Entries referencing the SI plus
	reclass JEs whose Cr-AR line allocates against it (migrate/invoices.py).
	`method` did not survive migration — null, the frontend tolerates it."""
	if not si_names:
		return {}
	out = {}
	pes = frappe.db.sql(
		"""select per.reference_name as si, pe.posting_date, pe.paid_amount,
			pe.reference_no
		from `tabPayment Entry Reference` per
		join `tabPayment Entry` pe on pe.name = per.parent
		where pe.docstatus = 1 and per.reference_doctype = 'Sales Invoice'
			and per.reference_name in %(names)s""",
		{"names": tuple(si_names)},
		as_dict=True,
	)
	for p in pes:
		out.setdefault(p.si, []).append(
			{
				"date": _iso_date(p.posting_date),
				"amount": float(p.paid_amount or 0),
				"method": None,
				"reference": p.reference_no,
				"_sort": str(p.posting_date or ""),
			}
		)
	jes = frappe.db.sql(
		"""select jea.reference_name as si, je.posting_date, je.cheque_no,
			jea.credit_in_account_currency as amount
		from `tabJournal Entry Account` jea
		join `tabJournal Entry` je on je.name = jea.parent
		where je.docstatus = 1 and jea.reference_type = 'Sales Invoice'
			and jea.reference_name in %(names)s
			and jea.credit_in_account_currency > 0""",
		{"names": tuple(si_names)},
		as_dict=True,
	)
	for p in jes:
		out.setdefault(p.si, []).append(
			{
				"date": _iso_date(p.posting_date),
				"amount": float(p.amount or 0),
				"method": None,
				"reference": p.cheque_no,
				"_sort": str(p.posting_date or ""),
			}
		)
	for rows in out.values():
		rows.sort(key=lambda r: r.pop("_sort"))
	return out


# ── shared helpers ──────────────────────────────────────────────────────────


def _si_amounts(si) -> tuple:
	"""(total, paid, outstanding, base status) from an SI row. Base status maps
	the legacy invoice enum: docstatus 0 -> DRAFT, 2 -> CANCELLED, else
	PAID / PARTIAL / SENT from the outstanding amount (payments already
	allocate against the SI, so grand_total − outstanding == sum(payments))."""
	total = float(si.grand_total or 0)
	if si.docstatus == 0:
		return total, 0.0, total, "DRAFT"
	if si.docstatus == 2:
		return total, 0.0, total, "CANCELLED"
	outstanding = float(si.outstanding_amount or 0)
	paid = total - outstanding
	if outstanding <= 0.005:
		return total, paid, outstanding, "PAID"
	if paid > 0.005:
		return total, paid, outstanding, "PARTIAL"
	return total, paid, outstanding, "SENT"


def _si_map(names: set) -> dict:
	if not names:
		return {}
	return {
		r.name: r
		for r in frappe.get_all(
			"Sales Invoice",
			filters={"name": ("in", list(names))},
			fields=[
				"name", "grand_total", "outstanding_amount", "docstatus",
				"posting_date", "due_date", "orion_legacy_id",
			],
		)
	}


def _resolve_project_name(ident: str) -> str | None:
	name = frappe.db.get_value("Project", {"orion_legacy_id": ident})
	if not name and frappe.db.exists("Project", ident):
		name = ident
	return name


def _project_legacy_map(names: set) -> dict:
	if not names:
		return {}
	return {
		r.name: (r.orion_legacy_id or r.name)
		for r in frappe.get_all(
			"Project",
			filters={"name": ("in", list(names))},
			fields=["name", "orion_legacy_id"],
		)
	}


def _program_legacy_map() -> dict:
	return {
		r.name: (r.orion_legacy_id or r.name)
		for r in frappe.get_all("Orion Program", fields=["name", "orion_legacy_id"])
	}


def _customer_names(names: set) -> dict:
	if not names:
		return {}
	return {
		r.name: r.customer_name
		for r in frappe.get_all(
			"Customer",
			filters={"name": ("in", list(names))},
			fields=["name", "customer_name"],
		)
	}


def _account_type_stub(acc) -> dict | None:
	"""{id, code, name, type} — the detail endpoint's line.account shape."""
	if not acc:
		return None
	return {
		"id": acc.orion_legacy_id or acc.name,
		"code": acc.account_number or "",
		"name": acc.account_name,
		"type": _orion_type(acc),
	}
