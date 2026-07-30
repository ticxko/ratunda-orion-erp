"""Accounting write compat handlers (invoices, bank loans, disbursements).

Mirrors the bellatrix-python routers so the feynman accounting screens keep
working against ERPNext data:

  /api/accounting/invoices       <- app/routers/accounting/invoices.py
                                    GET list/:id, POST, POST /:id/send,
                                    POST /:id/payment, PATCH /:id, DELETE /:id
  /api/accounting/bank-loans     <- app/routers/accounting/bank_loans.py
                                    GET list/:id, GET /:id/unlinked-lines,
                                    POST, POST /:id/drawdown, POST /:id/payment
  /api/accounting/disbursements  <- app/routers/accounting/disbursements.py
                                    GET list/:id, POST, PATCH /:id, DELETE /:id,
                                    POST /:id/approve /transfer /settle /cancel

Response shapes reproduce the source pydantic models verbatim (feynman
consumes them unchanged): InvoiceRead/InvoiceEnvelope/InvoiceList/InvoiceWithJE
(schemas/accounting/invoice.py), BankLoanRead/BankLoanEnvelope/BankLoanList/
LoanPaymentRead/LoanPaymentResponse/UnlinkedLine (schemas/accounting/loan.py),
DisbursementRead + Item/Target/Project stubs (schemas/accounting/disbursement.py).

Serialization parity: Numeric(15,2) Decimal fields go over the wire as 2dp JSON
*strings* (matching accounting.py / accounting_reports.py, which "%.2f" every
money field); datetimes as naive ISO, dates as midnight ISO. interestRate is a
fraction in the source (0.1088) but a Percent (10.88) in ERPNext, so it is
divided by 100 on the way out and multiplied by 100 on create.

Backing docs:
  invoice   -> Sales Invoice (+ items) / Payment Entry (payments)
  bank loan -> Orion Loan (+ Orion Loan Event children)
  cash adv. -> Orion Cash Advance (+ Orion Cash Advance Item children)

Every created Journal Entry balances and carries company + cost center per line
(reusing accounting.py's _post_journal_entry, which applies the same brand/
Shared cost-center rule as orion.migrate); JEs are inserted with
ignore_permissions and submitted. ids in payloads resolve via orion_legacy_id
first then ERPNext name; every response id is orion_legacy_id when present else
doc.name.
"""

from decimal import Decimal

import frappe

from orion.accounting.loan_codes import next_doc_code, recompute_outstanding
from orion.compat.accounting import (
	_account_by_number,
	_cc_by_line,
	_company,
	_insert_named,
	_iso,
	_iso_date,
	_legacy_or_name,
	_money,
	_parse_date,
	_post_journal_entry,
	_require_account,
	_resolve_account,
	_resolve_name,
	_resolve_project_or_throw,
	_s2,
	_s2n,
	_settings,
	_wdec,
)
from orion.compat.handle import route, split_path

# Local copies (kept decoupled from accounting.py on purpose).
INV_PREFIX = "/api/accounting/invoices"
LOAN_PREFIX = "/api/accounting/bank-loans"
DISB_PREFIX = "/api/accounting/disbursements"

AR_CODE = {"POIESIS_STUDIO": "1-1220", "RATUNDA_RENOVASI": "1-1210"}
REV_CODE = {"POIESIS_STUDIO": "4-2100", "RATUNDA_RENOVASI": "4-1100"}

DEFAULT_ACCOUNT_BY_LENDER = {
	"BANK": "2-1700",
	"OWNER": "2-1500",
	"THIRD_PARTY": "2-1800",
}
LOAN_INTEREST_CODE = "8-1100"

CATEGORY_GL = {
	"MATERIAL": "5-1100",
	"UPAH": "5-1200",
	"OPERASIONAL": "5-1600",
	"TRANSPORT": "5-1400",
}
KOPRA_GL = "1-1120"

_SI_FIELDS = [
	"name", "orion_legacy_id", "customer", "posting_date", "due_date",
	"grand_total", "net_total", "outstanding_amount", "docstatus", "status",
	"project", "creation", "modified", "debit_to",
]
_LOAN_FIELDS = [
	"name", "orion_legacy_id", "loan_number", "bank_name", "lender_type",
	"lender_name", "is_revolving", "is_company_liability", "doc_prefix",
	"account", "principal", "interest_rate", "monthly_installment",
	"tenor_months", "start_date", "maturity_date", "business_line", "notes",
	"creation", "modified",
]
_EVENT_FIELDS = [
	"name", "orion_legacy_id", "direction", "doc_code", "date",
	"principal_amount", "interest_amount", "total_amount", "remaining_balance",
	"journal_entry", "project", "channel", "reference", "notes", "creation",
]


# ── small shared helpers ─────────────────────────────────────────────────────


def _pdate(s):
	dt = _parse_date(s)
	return dt.date() if dt else None


def _dstr(v):
	"""Non-currency Decimal (volume) as a plain string, None-safe."""
	if v is None:
		return None
	return str(_wdec(v))


def _rate_frac(v):
	"""ERPNext Percent (10.88) -> source fraction string ("0.1088")."""
	if v is None:
		return None
	return str(_wdec(v) / Decimal("100"))


def _je_summary(je_name: str, source_id) -> dict:
	je = frappe.db.get_value(
		"Journal Entry",
		je_name,
		["name", "posting_date", "user_remark", "cheque_no", "orion_source_type", "orion_legacy_id"],
		as_dict=True,
	)
	if not je:
		return None
	return {
		"id": je.orion_legacy_id or je.name,
		"date": _iso_date(je.posting_date),
		"description": je.user_remark,
		"reference": je.cheque_no,
		"sourceType": je.orion_source_type,
		"sourceId": source_id,
	}


# ── invoices ─────────────────────────────────────────────────────────────────


@route(INV_PREFIX)
def invoices(path: str, verb: str, payload: dict):
	"""invoices.py — Sales Invoices carry orion_legacy_id; payments are Payment
	Entries / SI-referencing reclass JEs. Only SIs that exist are served."""
	bare, query = split_path(path)
	rest = bare[len(INV_PREFIX):].strip("/")
	parts = [p for p in rest.split("/") if p]
	if verb == "GET" and not parts:
		return _inv_list(query)
	if verb == "POST" and not parts:
		return {"invoice": _inv_create(payload)}
	if len(parts) == 1:
		if verb == "GET":
			return {"invoice": _inv_read_one(parts[0])}
		if verb == "PATCH":
			return {"invoice": _inv_update(parts[0], payload)}
		if verb == "DELETE":
			return _inv_delete(parts[0])
	if len(parts) == 2 and verb == "POST":
		if parts[1] == "send":
			return _inv_send(parts[0])
		if parts[1] == "payment":
			return _inv_payment(parts[0], payload)
	frappe.throw("No compat handler for %s %s" % (verb, bare), exc=frappe.DoesNotExistError)


def _si_status(si) -> str:
	"""Map ERPNext SI docstatus/outstanding back to the Orion invoice enum."""
	if si.docstatus == 2:
		return "CANCELLED"
	if si.docstatus == 0:
		return "DRAFT"
	if _wdec(si.outstanding_amount) <= Decimal("0.01"):
		return "PAID"
	if _wdec(si.grand_total) - _wdec(si.outstanding_amount) > 0:
		return "PARTIAL"
	return "SENT"


def _inv_bl_key(business_line: str) -> str:
	return "POIESIS_STUDIO" if business_line == "POIESIS_STUDIO" else "RATUNDA_RENOVASI"


def _bl_from_debit_to(debit_to: str) -> str:
	acc = frappe.db.get_value(
		"Account", debit_to, ["account_number", "orion_business_line"], as_dict=True
	)
	if acc:
		if acc.account_number == "1-1220":
			return "POIESIS_STUDIO"
		if acc.account_number == "1-1210":
			return "RATUNDA_RENOVASI"
		if acc.orion_business_line in ("POIESIS_STUDIO", "RATUNDA_RENOVASI"):
			return acc.orion_business_line
	return "RATUNDA_RENOVASI"


def _project_stub_inv(project_name):
	if not project_name:
		return None
	p = frappe.db.get_value(
		"Project",
		project_name,
		["orion_legacy_id", "project_name", "orion_business_line",
		 "client_salutation", "client_phone", "client_address"],
		as_dict=True,
	)
	if not p:
		return None
	return {
		"id": p.orion_legacy_id or project_name,
		"code": None,
		"name": p.project_name,
		"budget": None,
		"address": p.client_address,
		"businessLine": p.orion_business_line,
		"clientName": None,
		"clientSalutation": p.client_salutation,
		"clientPhone": p.client_phone,
	}


def _inv_payments(names: list, emitted_by: dict) -> dict:
	"""Reconstruct InvoicePaymentRead rows from Payment Entries referencing the
	SI plus reclass Journal Entries whose AR credit line references the SI."""
	result = {n: [] for n in names}

	refs = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": ("in", names), "docstatus": 1},
		fields=["parent", "reference_name", "allocated_amount"],
	)
	pe_names = list({r.parent for r in refs})
	pes = {}
	if pe_names:
		for p in frappe.get_all(
			"Payment Entry",
			filters={"name": ("in", pe_names)},
			fields=["name", "posting_date", "paid_to", "reference_no", "orion_receipt_code", "orion_legacy_id", "creation"],
		):
			pes[p.name] = p
	for r in refs:
		pe = pes.get(r.parent)
		if not pe:
			continue
		ref = pe.reference_no if (pe.reference_no and pe.reference_no != "-") else None
		result.setdefault(r.reference_name, []).append(
			{
				"id": pe.orion_legacy_id or pe.name,
				"invoiceId": emitted_by.get(r.reference_name),
				"date": _iso_date(pe.posting_date),
				"amount": _s2(r.allocated_amount),
				"bankAccountId": _legacy_or_name("Account", pe.paid_to),
				"method": "TRANSFER",
				"reference": ref,
				"receiptCode": pe.orion_receipt_code,
				"journalEntryId": pe.name,
				"notes": None,
				"createdAt": _iso(pe.creation),
			}
		)

	jelines = frappe.get_all(
		"Journal Entry Account",
		filters={"reference_type": "Sales Invoice", "reference_name": ("in", names), "parenttype": "Journal Entry"},
		fields=["parent", "reference_name", "credit_in_account_currency"],
	)
	je_names = list({l.parent for l in jelines if _wdec(l.credit_in_account_currency) > 0})
	if je_names:
		jes = {
			j.name: j
			for j in frappe.get_all(
				"Journal Entry",
				filters={"name": ("in", je_names), "docstatus": 1},
				fields=["name", "posting_date", "user_remark", "cheque_no", "orion_legacy_id", "creation"],
			)
		}
		fund = {}
		for a in frappe.get_all(
			"Journal Entry Account",
			filters={"parent": ("in", je_names)},
			fields=["parent", "account", "debit_in_account_currency"],
			order_by="idx asc",
		):
			if _wdec(a.debit_in_account_currency) > 0 and a.parent not in fund:
				fund[a.parent] = a.account
		for l in jelines:
			if _wdec(l.credit_in_account_currency) <= 0:
				continue
			je = jes.get(l.parent)
			if not je:
				continue
			funding = fund.get(l.parent)
			result.setdefault(l.reference_name, []).append(
				{
					"id": je.orion_legacy_id or je.name,
					"invoiceId": emitted_by.get(l.reference_name),
					"date": _iso_date(je.posting_date),
					"amount": _s2(l.credit_in_account_currency),
					"bankAccountId": _legacy_or_name("Account", funding) if funding else None,
					"method": "TRANSFER",
					"reference": je.cheque_no or None,
					"receiptCode": None,
					"journalEntryId": je.orion_legacy_id or je.name,
					"notes": None,
					"createdAt": _iso(je.creation),
				}
			)
	return result


def _inv_read_rows(rows: list) -> list:
	if not rows:
		return []
	names = [r.name for r in rows]
	emitted_by = {r.name: (r.orion_legacy_id or r.name) for r in rows}

	items_by = {}
	for it in frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": ("in", names)},
		fields=["name", "parent", "idx", "description", "item_name", "amount", "income_account", "orion_percentage", "creation"],
		order_by="parent asc, idx asc",
	):
		items_by.setdefault(it.parent, []).append(it)

	pays_by = _inv_payments(names, emitted_by)
	today = frappe.utils.today()

	out = []
	for si in rows:
		emitted = emitted_by[si.name]
		status = _si_status(si)
		bl = _bl_from_debit_to(si.debit_to)
		item_rows = items_by.get(si.name, [])
		inv_items = []
		for i, it in enumerate(item_rows):
			inv_items.append(
				{
					"id": it.name,
					"invoiceId": emitted,
					"description": it.description or it.item_name or "",
					"percentage": _s2n(it.orion_percentage) if it.orion_percentage is not None else None,
					"amount": _s2(it.amount),
					"sortOrder": i,
					"createdAt": _iso(it.creation),
				}
			)
		if si.docstatus == 1:
			paid = _wdec(si.grand_total) - _wdec(si.outstanding_amount)
			remaining = _wdec(si.outstanding_amount)
		else:
			paid = Decimal("0")
			remaining = _wdec(si.grand_total)
		effective = status
		if status == "SENT" and si.due_date and str(si.due_date) < today:
			effective = "OVERDUE"
		rev_acct = _legacy_or_name("Account", item_rows[0].income_account) if item_rows else None
		out.append(
			{
				"id": emitted,
				"number": si.name,
				"projectId": (_legacy_or_name("Project", si.project) if si.project else ""),
				"clientName": si.customer,
				"clientSalutation": None,
				"clientPhone": None,
				"clientAddress": None,
				"businessLine": bl,
				"issueDate": _iso_date(si.posting_date),
				"dueDate": _iso_date(si.due_date),
				"status": status,
				"subtotal": _s2(si.net_total if si.net_total else si.grand_total),
				"totalAmount": _s2(si.grand_total),
				"notes": None,
				"revenueAccountId": rev_acct,
				"clientId": None,
				"createdAt": _iso(si.creation),
				"updatedAt": _iso(si.modified),
				"items": inv_items,
				"payments": pays_by.get(si.name, []),
				"project": _project_stub_inv(si.project),
				"paidAmount": _s2(paid),
				"remaining": _s2(remaining),
				"effectiveStatus": effective,
			}
		)
	return out


def _inv_read_one(ident: str) -> dict:
	name = _resolve_name("Sales Invoice", ident)
	if not name:
		frappe.throw("Invoice not found", exc=frappe.DoesNotExistError)
	row = frappe.db.get_value("Sales Invoice", name, _SI_FIELDS, as_dict=True)
	return _inv_read_rows([row])[0]


def _inv_list(query: dict) -> dict:
	filters = {"company": _company()}
	if query.get("projectId"):
		pn = _resolve_name("Project", query["projectId"])
		filters["project"] = pn or "__no_such_project__"
	rows = frappe.get_all(
		"Sales Invoice", filters=filters, fields=_SI_FIELDS, order_by="creation desc"
	)
	reads = _inv_read_rows(rows)
	status_f = query.get("status")
	bl_f = query.get("businessLine")
	out = [
		r
		for r in reads
		if (not status_f or r["status"] == status_f) and (not bl_f or r["businessLine"] == bl_f)
	]
	return {"invoices": out}


def _ensure_receivable(account_name: str):
	if frappe.db.get_value("Account", account_name, "account_type") != "Receivable":
		frappe.db.set_value("Account", account_name, "account_type", "Receivable")


def _resolve_or_create_customer(client_name, client_id=None) -> str:
	if client_id:
		c = frappe.db.get_value("Customer", {"orion_legacy_id": client_id})
		if c:
			return c
	name = (client_name or "").strip() or "Unknown Client"
	c = frappe.db.get_value("Customer", {"customer_name": name})
	if c:
		return c
	from orion.migrate.customers import ensure_customer_group, ensure_territory

	doc = frappe.new_doc("Customer")
	doc.customer_name = name
	doc.customer_type = "Individual"
	doc.customer_group = ensure_customer_group()
	doc.territory = ensure_territory()
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _inv_create(payload: dict) -> dict:
	"""POST '' — a draft Sales Invoice mirroring send_invoice defaults (never
	submitted). debit_to = brand AR account (1-1220 Poiesis / 1-1210 else);
	income = revenueAccountId else brand revenue (4-2100 / 4-1100)."""
	project_name = _resolve_project_or_throw(payload.get("projectId"))
	bl_key = _inv_bl_key(payload.get("businessLine") or "RATUNDA_RENOVASI")

	ar = _account_by_number(AR_CODE[bl_key])
	if not ar:
		frappe.throw("AR account %s not found" % AR_CODE[bl_key])
	if payload.get("revenueAccountId"):
		rev = _require_account(payload["revenueAccountId"]).name
	else:
		rev_row = _account_by_number(REV_CODE[bl_key])
		if not rev_row:
			frappe.throw("Revenue account %s not found" % REV_CODE[bl_key])
		rev = rev_row.name

	number = _inv_number(project_name, bl_key)
	customer = _resolve_or_create_customer(payload.get("clientName"), payload.get("clientId"))
	posting = _pdate(payload.get("issueDate"))
	due = _pdate(payload.get("dueDate")) or posting
	if due and posting and due < posting:
		due = posting

	cc = _cc_by_line().get(bl_key) or _settings().shared_cost_center
	_ensure_receivable(ar.name)

	si = frappe.new_doc("Sales Invoice")
	si.name = number
	si.customer = customer
	si.company = _company()
	si.currency = "IDR"
	si.conversion_rate = 1
	si.set_posting_time = 1
	si.posting_date = posting
	si.due_date = due
	si.debit_to = ar.name
	si.update_stock = 0
	si.disable_rounded_total = 1
	si.project = project_name
	for it in payload.get("items") or []:
		label = (it.get("description") or "").strip() or "Invoice item"
		si.append(
			"items",
			{
				"item_name": label[:140],
				"description": label,
				"qty": 1,
				"uom": "Nos",
				"rate": _money(_wdec(it.get("amount"))),
				"income_account": rev,
				"cost_center": cc,
				"project": project_name,
				"orion_percentage": _money(it.get("percentage")) if it.get("percentage") is not None else None,
			},
		)
	si.flags.ignore_permissions = True
	_insert_named(si)
	return _inv_read_one(si.name)


def _inv_number(project_name: str, bl_key: str) -> str:
	p = frappe.db.get_value(
		"Project",
		project_name,
		["orion_business_line", "kn_sequence", "kontrak_yymm", "lctr", "orion_service_type"],
		as_dict=True,
	) or frappe._dict()
	bl = "R" if (p.orion_business_line or "RATUNDA_RENOVASI") == "RATUNDA_RENOVASI" else "P"
	count = frappe.db.count("Sales Invoice", {"project": project_name})
	seq = "%02d" % (int(count) + 1)
	kn = "KN%02d" % int(p.kn_sequence or 0)
	yymm = p.kontrak_yymm or "0000"
	lctr = p.lctr or "XXXX"
	svc = p.orion_service_type or "UNKWN"
	return "%s-INV%s-%s-%s-%s-%s" % (bl, seq, kn, yymm, lctr, svc)


def _inv_update(ident: str, payload: dict) -> dict:
	name = _resolve_name("Sales Invoice", ident)
	if not name:
		frappe.throw("Invoice not found", exc=frappe.DoesNotExistError)
	si = frappe.get_doc("Sales Invoice", name)
	if si.docstatus != 0:
		frappe.throw("Only DRAFT invoices can be edited")

	si.set_posting_time = 1
	if payload.get("issueDate"):
		si.posting_date = _pdate(payload["issueDate"])
	if payload.get("dueDate"):
		si.due_date = _pdate(payload["dueDate"])
	if si.due_date and si.posting_date and frappe.utils.getdate(si.due_date) < frappe.utils.getdate(si.posting_date):
		si.due_date = si.posting_date

	if payload.get("items") is not None:
		bl_key = _bl_from_debit_to(si.debit_to)
		if payload.get("revenueAccountId"):
			rev = _require_account(payload["revenueAccountId"]).name
		elif si.items:
			rev = si.items[0].income_account
		else:
			rev_row = _account_by_number(REV_CODE[bl_key])
			rev = rev_row.name if rev_row else None
		cc = _cc_by_line().get(bl_key) or _settings().shared_cost_center
		si.set("items", [])
		for it in payload["items"]:
			label = (it.get("description") or "").strip() or "Invoice item"
			si.append(
				"items",
				{
					"item_name": label[:140],
					"description": label,
					"qty": 1,
					"uom": "Nos",
					"rate": _money(_wdec(it.get("amount"))),
					"income_account": rev,
					"cost_center": cc,
					"project": si.project,
					"orion_percentage": _money(it.get("percentage")) if it.get("percentage") is not None else None,
				},
			)
	si.flags.ignore_permissions = True
	si.save()
	return _inv_read_one(name)


def _inv_delete(ident: str) -> dict:
	name = _resolve_name("Sales Invoice", ident)
	if not name:
		frappe.throw("Invoice not found", exc=frappe.DoesNotExistError)
	si = frappe.get_doc("Sales Invoice", name)
	if si.docstatus != 0:
		frappe.throw("Only DRAFT invoices can be deleted")
	frappe.delete_doc("Sales Invoice", name, force=1, ignore_permissions=True)
	return {"success": True}


def _inv_send(ident: str) -> dict:
	"""POST /:id/send — submit the SI (posts Dr AR / Cr income GL)."""
	name = _resolve_name("Sales Invoice", ident)
	if not name:
		frappe.throw("Invoice not found", exc=frappe.DoesNotExistError)
	si = frappe.get_doc("Sales Invoice", name)
	if si.docstatus != 0:
		frappe.throw("Only DRAFT invoices can be sent")
	_ensure_receivable(si.debit_to)
	si.flags.ignore_permissions = True
	si.submit()
	read = _inv_read_one(name)
	return {
		"invoice": read,
		"journalEntry": {
			"id": si.name,
			"date": _iso_date(si.posting_date),
			"description": "Invoice %s — %s" % (si.name, si.customer),
			"reference": si.name,
			"sourceType": "AR_INVOICE",
			"sourceId": read["id"],
		},
	}


def _inv_payment(ident: str, payload: dict) -> dict:
	"""POST /:id/payment — a submitted Payment Entry (Receive): paid_from = SI
	debit_to (AR), paid_to = payload account, party = customer, allocated to SI."""
	name = _resolve_name("Sales Invoice", ident)
	if not name:
		frappe.throw("Invoice not found", exc=frappe.DoesNotExistError)
	si = frappe.get_doc("Sales Invoice", name)
	status = _si_status(si)
	if status in ("DRAFT", "CANCELLED", "PAID"):
		frappe.throw("Cannot record payment for %s invoice" % status)

	amount = _wdec(payload.get("amount"))
	outstanding = _wdec(si.outstanding_amount)
	if amount > outstanding + Decimal("0.01"):
		frappe.throw(
			"Payment amount (%s) exceeds remaining balance (%s)" % (amount, outstanding)
		)
	paid_to = _require_account(payload.get("bankAccountId"))
	pay_dt = _pdate(payload.get("date"))
	allocated = min(amount, outstanding)

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.company = _company()
	pe.posting_date = pay_dt
	pe.party_type = "Customer"
	pe.party = si.customer
	pe.paid_from = si.debit_to
	pe.paid_to = paid_to.name
	pe.paid_from_account_currency = "IDR"
	pe.paid_to_account_currency = "IDR"
	pe.source_exchange_rate = 1
	pe.target_exchange_rate = 1
	pe.paid_amount = _money(amount)
	pe.received_amount = _money(amount)
	pe.reference_no = payload.get("reference") or si.name
	pe.reference_date = pay_dt
	pe.cost_center = _settings().shared_cost_center
	pe.orion_receipt_code = payload.get("receiptCode")
	pe.append(
		"references",
		{
			"reference_doctype": "Sales Invoice",
			"reference_name": si.name,
			"total_amount": si.grand_total,
			"outstanding_amount": si.outstanding_amount,
			"allocated_amount": _money(allocated),
		},
	)
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()

	read = _inv_read_one(name)
	return {
		"invoice": read,
		"journalEntry": {
			"id": pe.name,
			"date": _iso_date(pay_dt),
			"description": "Pembayaran %s — %s" % (si.name, si.customer),
			"reference": payload.get("reference") or si.name,
			"sourceType": "AR_INVOICE",
			"sourceId": read["id"],
		},
	}


# ── bank loans ───────────────────────────────────────────────────────────────


@route(LOAN_PREFIX)
def bank_loans(path: str, verb: str, payload: dict):
	"""bank_loans.py — over Orion Loan (+ Orion Loan Event); outstanding is
	always recomputed from events (orion.accounting.loan_codes)."""
	bare, query = split_path(path)
	rest = bare[len(LOAN_PREFIX):].strip("/")
	parts = [p for p in rest.split("/") if p]
	if verb == "GET" and not parts:
		return _loan_list(query)
	if verb == "POST" and not parts:
		return {"loan": _loan_create(payload)}
	if len(parts) == 1 and verb == "GET":
		return {"loan": _loan_read(_loan_name(parts[0]))}
	if len(parts) == 2 and parts[1] == "unlinked-lines" and verb == "GET":
		return _loan_unlinked(parts[0])
	if len(parts) == 2 and verb == "POST":
		if parts[1] == "drawdown":
			return _loan_drawdown(parts[0], payload)
		if parts[1] == "payment":
			return _loan_repayment(parts[0], payload)
	frappe.throw("No compat handler for %s %s" % (verb, bare), exc=frappe.DoesNotExistError)


def _loan_name(ident: str) -> str:
	name = _resolve_name("Orion Loan", ident)
	if not name:
		frappe.throw("Loan not found", exc=frappe.DoesNotExistError)
	return name


def _loan_outstanding(name: str) -> Decimal:
	rows = frappe.db.sql(
		"""select direction, coalesce(sum(principal_amount), 0)
		from `tabOrion Loan Event` where loan = %s group by direction""",
		name,
	)
	return recompute_outstanding(rows)


def _sync_loan(name: str, is_revolving) -> Decimal:
	outstanding = _loan_outstanding(name)
	if is_revolving:
		status = "ACTIVE"
	else:
		status = "PAID_OFF" if outstanding <= Decimal("0.01") else "ACTIVE"
	frappe.db.set_value("Orion Loan", name, "status", status)
	return outstanding


def _loan_event_read(e, loan_emitted: str) -> dict:
	return {
		"id": e.orion_legacy_id or e.name,
		"bankLoanId": loan_emitted,
		"direction": e.direction,
		"docCode": e.doc_code,
		"date": _iso_date(e.date),
		"principalAmount": _s2(e.principal_amount),
		"interestAmount": _s2(e.interest_amount),
		"totalAmount": _s2(e.total_amount),
		"remainingBalance": _s2(e.remaining_balance),
		"bankAccountId": None,
		"journalEntryId": _legacy_or_name("Journal Entry", e.journal_entry) if e.journal_entry else None,
		"projectId": _legacy_or_name("Project", e.project) if e.project else None,
		"channel": e.channel,
		"reference": e.reference,
		"notes": e.notes,
		"createdAt": _iso(e.creation),
	}


def _loan_read(name: str, with_aggregates: bool = True) -> dict:
	h = frappe.db.get_value("Orion Loan", name, _LOAN_FIELDS, as_dict=True)
	emitted = h.orion_legacy_id or name
	outstanding = _loan_outstanding(name)
	if h.is_revolving:
		status = "ACTIVE"
	else:
		status = "PAID_OFF" if outstanding <= Decimal("0.01") else "ACTIVE"

	events = frappe.get_all(
		"Orion Loan Event",
		filters={"loan": name},
		fields=_EVENT_FIELDS,
		order_by="date desc, creation desc",
	)
	out = {
		"id": emitted,
		"loanNumber": h.loan_number,
		"bankName": h.bank_name,
		"lenderType": h.lender_type,
		"lenderName": h.lender_name,
		"isRevolving": bool(h.is_revolving),
		"isCompanyLiability": bool(h.is_company_liability),
		"docPrefix": h.doc_prefix,
		"accountId": (_legacy_or_name("Account", h.account) if h.account else ""),
		"principal": _s2(h.principal),
		"interestRate": _rate_frac(h.interest_rate),
		"monthlyInstallment": _s2n(h.monthly_installment),
		"tenorMonths": h.tenor_months,
		"startDate": _iso_date(h.start_date),
		"maturityDate": _iso_date(h.maturity_date) if h.maturity_date else None,
		"outstandingBalance": _s2(outstanding),
		"status": status,
		"businessLine": h.business_line,
		"notes": h.notes,
		"createdAt": _iso(h.creation),
		"updatedAt": _iso(h.modified),
		"payments": [_loan_event_read(e, emitted) for e in events],
		"totalPaid": None,
		"totalInterestPaid": None,
		"totalDrawn": None,
		"paymentCount": None,
		"drawdownCount": None,
	}
	if with_aggregates:
		rep = [e for e in events if e.direction == "REPAYMENT"]
		dr = [e for e in events if e.direction == "DRAWDOWN"]
		out["totalPaid"] = _s2(sum((_wdec(e.principal_amount) for e in rep), Decimal("0")))
		out["totalInterestPaid"] = _s2(sum((_wdec(e.interest_amount) for e in rep), Decimal("0")))
		out["totalDrawn"] = _s2(sum((_wdec(e.principal_amount) for e in dr), Decimal("0")))
		out["paymentCount"] = len(rep)
		out["drawdownCount"] = len(dr)
	return out


def _loan_list(query: dict) -> dict:
	filters = {}
	if query.get("lenderType"):
		filters["lender_type"] = query["lenderType"]
	names = frappe.get_all(
		"Orion Loan", filters=filters, order_by="creation desc", pluck="name"
	)
	return {"loans": [_loan_read(n) for n in names]}


def _resolve_loan_account(payload_account_id, lender_type: str) -> str:
	if payload_account_id:
		acc = _resolve_account(payload_account_id)
		if not acc:
			frappe.throw("Loan account not found: %s" % payload_account_id, exc=frappe.DoesNotExistError)
		return acc.name
	code = DEFAULT_ACCOUNT_BY_LENDER.get(lender_type, "2-1700")
	acc = _account_by_number(code)
	if not acc:
		frappe.throw("Default loan account %s not found" % code)
	return acc.name


def _loan_create(payload: dict) -> dict:
	lender_type = payload.get("lenderType") or "BANK"
	if lender_type not in DEFAULT_ACCOUNT_BY_LENDER:
		frappe.throw("Invalid lenderType")
	if not payload.get("startDate"):
		frappe.throw("startDate is required")
	if lender_type == "BANK":
		missing = [
			label
			for label, val in [
				("loanNumber", payload.get("loanNumber")),
				("bankName", payload.get("bankName")),
				("principal", payload.get("principal")),
				("monthlyInstallment", payload.get("monthlyInstallment")),
				("tenorMonths", payload.get("tenorMonths")),
				("maturityDate", payload.get("maturityDate")),
			]
			if not val
		]
		if missing:
			frappe.throw("For a BANK loan these are required: %s" % ", ".join(missing))
	if not payload.get("lenderName") and not payload.get("bankName"):
		frappe.throw("lenderName (or bankName) is required")

	is_rev = payload.get("isRevolving")
	is_revolving = is_rev if is_rev is not None else (lender_type != "BANK")

	doc = frappe.new_doc("Orion Loan")
	doc.loan_number = payload.get("loanNumber") or payload.get("lenderName") or payload.get("bankName")
	doc.bank_name = payload.get("bankName")
	doc.lender_type = lender_type
	doc.lender_name = payload.get("lenderName") or payload.get("bankName")
	doc.is_revolving = 1 if is_revolving else 0
	doc.is_company_liability = 1 if payload.get("isCompanyLiability", True) else 0
	doc.doc_prefix = payload.get("docPrefix")
	doc.account = _resolve_loan_account(payload.get("accountId"), lender_type)
	doc.principal = _money(payload.get("principal")) or 0
	if payload.get("interestRate") is not None:
		doc.interest_rate = float(_wdec(payload.get("interestRate")) * 100)
	doc.monthly_installment = _money(payload.get("monthlyInstallment"))
	doc.tenor_months = payload.get("tenorMonths")
	doc.start_date = _pdate(payload.get("startDate"))
	doc.maturity_date = _pdate(payload.get("maturityDate")) if payload.get("maturityDate") else None
	doc.status = "ACTIVE"
	doc.business_line = payload.get("businessLine") or "ALL"
	doc.notes = payload.get("notes")
	doc.flags.ignore_permissions = True
	doc.insert()
	return _loan_read(doc.name, with_aggregates=False)


def _loan_unlinked(ident: str) -> dict:
	"""GET /:id/unlinked-lines — GL lines on the loan's liability account that
	no Orion Loan Event backs (UnlinkedLineList)."""
	name = _loan_name(ident)
	account = frappe.db.get_value("Orion Loan", name, "account")
	if not account:
		return {"lines": []}
	linked = set(
		frappe.get_all(
			"Orion Loan Event", filters={"journal_entry": ("is", "set")}, pluck="journal_entry"
		)
	)
	line_rows = frappe.get_all(
		"Journal Entry Account",
		filters={"account": account, "parenttype": "Journal Entry"},
		fields=["parent", "debit_in_account_currency", "credit_in_account_currency"],
	)
	parents = [l.parent for l in line_rows if l.parent not in linked]
	heads = {}
	if parents:
		for h in frappe.get_all(
			"Journal Entry",
			filters={"name": ("in", parents), "docstatus": 1},
			fields=["name", "posting_date", "user_remark", "cheque_no", "orion_legacy_id"],
			order_by="posting_date desc",
		):
			heads[h.name] = h
	lines = []
	for l in line_rows:
		h = heads.get(l.parent)
		if not h:
			continue
		credit = _wdec(l.credit_in_account_currency)
		lines.append(
			{
				"journalEntryId": h.orion_legacy_id or h.name,
				"date": _iso_date(h.posting_date),
				"description": h.user_remark,
				"reference": h.cheque_no,
				"debit": _s2(l.debit_in_account_currency),
				"credit": _s2(l.credit_in_account_currency),
				"suggestedDirection": "DRAWDOWN" if credit > 0 else "REPAYMENT",
			}
		)
	return {"lines": lines}


def _loan_drawdown(ident: str, payload: dict) -> dict:
	"""POST /:id/drawdown — Dr bank / Cr loan liability; link the event to the
	JE. Two modes: link an existing JE (journalEntryId) or post a new one."""
	name = _loan_name(ident)
	loan = frappe.get_doc("Orion Loan", name)
	amount = _wdec(payload.get("amount"))
	if amount <= 0:
		frappe.throw("amount must be positive")
	when = _pdate(payload.get("date"))
	if not when:
		frappe.throw("date is required")

	je_id = payload.get("journalEntryId")
	if je_id:
		je_name = _resolve_name("Journal Entry", je_id)
		if not je_name:
			frappe.throw("Journal entry not found")
		if frappe.db.exists("Orion Loan Event", {"journal_entry": je_name}):
			frappe.throw("Journal entry is already linked to a loan event")
		if not loan.account or not frappe.db.exists(
			"Journal Entry Account", {"parent": je_name, "account": loan.account}
		):
			frappe.throw("Journal entry does not touch this loan's liability account")
	else:
		if not payload.get("bankAccountId"):
			frappe.throw("bankAccountId is required when not linking an existing journal entry")
		if not loan.account:
			frappe.throw("Loan has no liability account configured")
		bank = _require_account(payload.get("bankAccountId"))
		lender = loan.lender_name or loan.bank_name or ""
		je = _post_journal_entry(
			posting_date=when,
			description="Pencairan pinjaman — %s" % lender,
			reference=payload.get("reference"),
			source_type="LOAN_DRAWDOWN",
			lines=[
				{"account": bank.name, "debit": amount, "credit": Decimal("0"),
				 "description": ("Penerimaan pinjaman — %s" % lender).strip()},
				{"account": loan.account, "debit": Decimal("0"), "credit": amount,
				 "description": ("Penambahan pinjaman — %s" % lender).strip()},
			],
			project=_resolve_project_or_throw(payload.get("projectId")) if payload.get("projectId") else None,
		)
		je_name = je.name

	doc_code = payload.get("docCode") or next_doc_code(
		loan=name, direction="DRAWDOWN", when=when, prefix=loan.doc_prefix
	)
	ev = frappe.new_doc("Orion Loan Event")
	ev.loan = name
	ev.direction = "DRAWDOWN"
	ev.doc_code = doc_code
	ev.date = when
	ev.principal_amount = _money(amount)
	ev.interest_amount = 0
	ev.total_amount = _money(amount)
	ev.remaining_balance = 0
	ev.journal_entry = je_name
	ev.project = _resolve_name("Project", payload.get("projectId")) if payload.get("projectId") else None
	ev.channel = payload.get("channel")
	ev.reference = payload.get("reference")
	ev.notes = payload.get("notes")
	ev.flags.ignore_permissions = True
	ev.insert()
	outstanding = _sync_loan(name, loan.is_revolving)
	frappe.db.set_value("Orion Loan Event", ev.name, "remaining_balance", _money(outstanding))
	return {"loan": _loan_read(name), "journalEntry": _je_summary(je_name, _legacy_or_name("Orion Loan", name))}


def _loan_repayment(ident: str, payload: dict) -> dict:
	"""POST /:id/payment — Dr loan liability (+ Dr interest 8-1100) / Cr bank."""
	name = _loan_name(ident)
	loan = frappe.get_doc("Orion Loan", name)
	outstanding_before = _loan_outstanding(name)
	current_status = "ACTIVE" if loan.is_revolving else (
		"PAID_OFF" if outstanding_before <= Decimal("0.01") else "ACTIVE"
	)
	if current_status != "ACTIVE":
		frappe.throw("Loan is already paid off")
	if (
		not payload.get("date")
		or payload.get("principalAmount") is None
		or payload.get("interestAmount") is None
		or not payload.get("bankAccountId")
	):
		frappe.throw("date, principalAmount, interestAmount, and bankAccountId are required")

	principal = _wdec(payload.get("principalAmount"))
	interest = _wdec(payload.get("interestAmount"))
	total = principal + interest
	if principal - outstanding_before > Decimal("0.01"):
		frappe.throw("Principal amount exceeds outstanding balance (%s)" % outstanding_before)

	bank = _require_account(payload.get("bankAccountId"))
	when = _pdate(payload.get("date"))

	lines = []
	if principal > 0:
		if not loan.account:
			frappe.throw("Loan has no liability account configured")
		lines.append(
			{"account": loan.account, "debit": principal, "credit": Decimal("0"),
			 "description": ("Cicilan pokok — %s" % (loan.lender_name or loan.loan_number or "")).strip()}
		)
	if interest > 0:
		ia = _account_by_number(LOAN_INTEREST_CODE)
		if not ia:
			frappe.throw("Interest expense account %s not found" % LOAN_INTEREST_CODE)
		lines.append(
			{"account": ia.name, "debit": interest, "credit": Decimal("0"),
			 "description": ("Bunga pinjaman — %s" % (loan.loan_number or loan.lender_name or "")).strip()}
		)
	lines.append(
		{"account": bank.name, "debit": Decimal("0"), "credit": total,
		 "description": ("Pembayaran pinjaman — %s" % (loan.lender_name or loan.loan_number or "")).strip()}
	)
	je = _post_journal_entry(
		posting_date=when,
		description=("Cicilan %s — %s" % (loan.loan_number or "", loan.lender_name or loan.bank_name or "")).strip(),
		reference=payload.get("reference") or loan.loan_number,
		source_type="LOAN_PAYMENT",
		lines=lines,
		project=_resolve_project_or_throw(payload.get("projectId")) if payload.get("projectId") else None,
	)

	# Orion Loan Event autonames from doc_code, so one is always allocated.
	doc_code = payload.get("docCode") or next_doc_code(
		loan=name, direction="REPAYMENT", when=when, prefix=None
	)
	ev = frappe.new_doc("Orion Loan Event")
	ev.loan = name
	ev.direction = "REPAYMENT"
	ev.doc_code = doc_code
	ev.date = when
	ev.principal_amount = _money(principal)
	ev.interest_amount = _money(interest)
	ev.total_amount = _money(total)
	ev.remaining_balance = 0
	ev.journal_entry = je.name
	ev.project = _resolve_name("Project", payload.get("projectId")) if payload.get("projectId") else None
	ev.channel = payload.get("channel")
	ev.reference = payload.get("reference")
	ev.notes = payload.get("notes")
	ev.flags.ignore_permissions = True
	ev.insert()
	outstanding = _sync_loan(name, loan.is_revolving)
	frappe.db.set_value("Orion Loan Event", ev.name, "remaining_balance", _money(outstanding))
	return {"loan": _loan_read(name), "journalEntry": _je_summary(je.name, _legacy_or_name("Orion Loan", name))}


# ── disbursements (cash advances) ────────────────────────────────────────────


@route(DISB_PREFIX)
def disbursements(path: str, verb: str, payload: dict):
	"""disbursements.py — over Orion Cash Advance (+ items). State machine
	DRAFT -> APPROVED -> TRANSFERRED -> SETTLED (+ CANCELLED); /transfer posts
	the CASH_ADVANCE journal entry."""
	bare, query = split_path(path)
	rest = bare[len(DISB_PREFIX):].strip("/")
	parts = [p for p in rest.split("/") if p]
	if verb == "GET" and not parts:
		return _disb_list(query)
	if verb == "POST" and not parts:
		return _disb_create(payload)
	if len(parts) == 1:
		if verb == "GET":
			return _disb_read(_disb_name(parts[0]), with_je=True)
		if verb == "PATCH":
			return _disb_update(parts[0], payload)
		if verb == "DELETE":
			return _disb_delete(parts[0])
	if len(parts) == 2 and verb == "POST":
		action = parts[1]
		if action == "approve":
			return _disb_transition(parts[0], "DRAFT", "APPROVED", "Only DRAFT disbursements can be approved")
		if action == "transfer":
			return _disb_transfer(parts[0], payload)
		if action == "settle":
			return _disb_transition(parts[0], "TRANSFERRED", "SETTLED", "Only TRANSFERRED disbursements can be settled")
		if action == "cancel":
			return _disb_cancel(parts[0])
	frappe.throw("No compat handler for %s %s" % (verb, bare), exc=frappe.DoesNotExistError)


def _disb_name(ident: str) -> str:
	name = _resolve_name("Orion Cash Advance", ident)
	if not name:
		frappe.throw("Not found", exc=frappe.DoesNotExistError)
	return name


def _project_stub_disb(project_name):
	if not project_name:
		return None
	p = frappe.db.get_value(
		"Project", project_name, ["orion_legacy_id", "project_name", "lctr", "kn_sequence"], as_dict=True
	)
	if not p:
		return None
	return {
		"id": p.orion_legacy_id or project_name,
		"code": None,
		"name": p.project_name,
		"lctr": p.lctr,
		"knSequence": p.kn_sequence,
	}


def _target_account_stub(bank_account_name):
	if not bank_account_name:
		return None
	ba = frappe.db.get_value(
		"Bank Account", bank_account_name, ["account_name", "bank", "bank_account_no", "orion_legacy_id"], as_dict=True
	)
	if not ba:
		return None
	return {
		"id": ba.orion_legacy_id or bank_account_name,
		"accountHolderName": ba.account_name,
		"bankName": ba.bank,
		"accountNumber": ba.bank_account_no,
	}


def _disb_item(i, emitted: str) -> dict:
	return {
		"id": i.name,
		"disbursementId": emitted,
		"tahapPekerjaan": i.tahap_pekerjaan or "",
		"item": i.item,
		"spekUkuran": i.spek_ukuran,
		"volume": _dstr(i.volume),
		"satuan": i.satuan,
		"durasiValue": i.durasi_value,
		"durasiUnit": i.durasi_unit,
		"category": i.category,
		"unitPrice": _s2n(i.unit_price),
		"amount": _s2(i.amount),
		"glAccountCode": i.gl_account_code,
		"sortOrder": i.sort_order or 0,
		"createdAt": _iso(i.creation),
	}


def _disb_je(je_name: str, emitted: str):
	je = frappe.db.get_value(
		"Journal Entry", je_name,
		["name", "posting_date", "user_remark", "cheque_no", "orion_source_type", "orion_legacy_id"],
		as_dict=True,
	)
	if not je:
		return None
	lines = frappe.get_all(
		"Journal Entry Account",
		filters={"parent": je_name},
		fields=["name", "account", "debit_in_account_currency", "credit_in_account_currency", "user_remark"],
		order_by="idx asc",
	)
	acc_ids = {l.account for l in lines if l.account}
	accs = {}
	if acc_ids:
		for a in frappe.get_all(
			"Account", filters={"name": ("in", list(acc_ids))},
			fields=["name", "account_number", "account_name", "orion_legacy_id"],
		):
			accs[a.name] = a
	out_lines = []
	for l in lines:
		a = accs.get(l.account)
		out_lines.append(
			{
				"id": l.name,
				"accountId": (a.orion_legacy_id or a.name) if a else l.account,
				"debit": _s2(l.debit_in_account_currency),
				"credit": _s2(l.credit_in_account_currency),
				"description": l.user_remark,
				"account": ({"id": a.orion_legacy_id or a.name, "code": a.account_number or "", "name": a.account_name} if a else None),
			}
		)
	return {
		"id": je.orion_legacy_id or je.name,
		"date": _iso_date(je.posting_date),
		"description": je.user_remark,
		"reference": je.cheque_no,
		"sourceType": je.orion_source_type,
		"sourceId": emitted,
		"lines": out_lines,
	}


def _disb_read(name: str, with_je: bool = False) -> dict:
	d = frappe.get_doc("Orion Cash Advance", name)
	emitted = d.orion_legacy_id or d.name
	items = sorted(d.items, key=lambda i: (i.sort_order or 0))
	return {
		"id": emitted,
		"code": d.code,
		"projectId": (_legacy_or_name("Project", d.project) if d.project else ""),
		"businessLine": d.business_line,
		"caType": d.ca_type,
		"title": d.title,
		"requestedBy": d.requested_by,
		"preparedBy": d.prepared_by,
		"status": d.status,
		"targetAccountId": (_legacy_or_name("Bank Account", d.target_bank_account) if d.target_bank_account else ""),
		"transferDate": _iso_date(d.transfer_date) if d.transfer_date else None,
		"keteranganTransfer": d.keterangan_transfer,
		"totalAmount": _s2(d.total_amount),
		"journalEntryId": (_legacy_or_name("Journal Entry", d.journal_entry) if d.journal_entry else None),
		"sourceFile": d.source_file,
		"physicalId": d.physical_id,
		"notes": d.notes,
		"createdAt": _iso(d.creation),
		"updatedAt": _iso(d.modified),
		"project": _project_stub_disb(d.project),
		"targetAccount": _target_account_stub(d.target_bank_account),
		"items": [_disb_item(i, emitted) for i in items],
		"fieldReceipts": [],
		"journalEntry": _disb_je(d.journal_entry, emitted) if (with_je and d.journal_entry) else None,
	}


def _disb_list(query: dict) -> list:
	filters = {}
	if query.get("status"):
		filters["status"] = query["status"]
	if query.get("caType"):
		filters["ca_type"] = query["caType"]
	if query.get("projectId"):
		pn = _resolve_name("Project", query["projectId"])
		filters["project"] = pn or "__no_such_project__"
	if query.get("from"):
		filters["creation"] = [">=", _parse_date(query["from"])]
	if query.get("to"):
		end = _parse_date(query["to"])
		if end:
			end = end.replace(hour=23, minute=59, second=59)
			filters.setdefault("creation", None)
			if isinstance(filters.get("creation"), list):
				# combine into a between if from also present
				lo = filters["creation"][1]
				filters["creation"] = ["between", [lo, end]]
			else:
				filters["creation"] = ["<=", end]
	names = frappe.get_all(
		"Orion Cash Advance", filters=filters, order_by="creation desc", pluck="name"
	)
	return [_disb_read(n) for n in names]


def _ca_code(project_name: str, ca_type: str) -> str:
	p = frappe.db.get_value(
		"Project", project_name,
		["orion_business_line", "kn_sequence", "kontrak_yymm", "lctr", "orion_service_type"],
		as_dict=True,
	) or frappe._dict()
	bl = "R" if (p.orion_business_line or "RATUNDA_RENOVASI") == "RATUNDA_RENOVASI" else "P"
	count = frappe.db.count("Orion Cash Advance", {"project": project_name, "ca_type": ca_type})
	seq = "%02d" % (int(count) + 1)
	kn = "KN%02d" % int(p.kn_sequence or 0)
	yymm = p.kontrak_yymm or "0000"
	lctr = p.lctr or "XXXX"
	svc = p.orion_service_type or "UNKWN"
	return "%s-%s%s-%s-%s-%s-%s" % (bl, ca_type, seq, kn, yymm, lctr, svc)


def _disb_item_row(i, idx: int) -> dict:
	return {
		"tahap_pekerjaan": i.get("tahapPekerjaan") or "",
		"item": i.get("item"),
		"spek_ukuran": i.get("spekUkuran"),
		"volume": float(i["volume"]) if i.get("volume") is not None else None,
		"satuan": i.get("satuan"),
		"durasi_value": str(i["durasiValue"]) if i.get("durasiValue") is not None else None,
		"durasi_unit": i.get("durasiUnit"),
		"category": i.get("category"),
		"unit_price": _money(i.get("unitPrice")),
		"amount": _money(i.get("amount")),
		"gl_account_code": i.get("glAccountCode"),
		"sort_order": idx,
	}


def _disb_create(payload: dict) -> dict:
	valid_ca = {"CAU", "CAM", "CAO"}
	ca_type = payload.get("caType") if payload.get("caType") in valid_ca else "CAU"

	project_name = _resolve_name("Project", payload.get("projectId"))
	if not project_name:
		frappe.throw("Project not found", exc=frappe.DoesNotExistError)
	target = _resolve_name("Bank Account", payload.get("targetAccountId"))
	if not target:
		frappe.throw("Target bank account not found", exc=frappe.DoesNotExistError)

	code = _ca_code(project_name, ca_type)
	items = payload.get("items") or []
	total = sum((_wdec(i.get("amount")) for i in items), Decimal("0"))
	title = payload.get("title")
	keterangan = payload.get("keteranganTransfer") or ("%s %s" % (code, title))
	bl = frappe.db.get_value("Project", project_name, "orion_business_line") or "RATUNDA_RENOVASI"

	d = frappe.new_doc("Orion Cash Advance")
	d.code = code
	d.project = project_name
	d.business_line = bl
	d.ca_type = ca_type
	d.title = title
	d.requested_by = payload.get("requestedBy")
	d.prepared_by = payload.get("preparedBy")
	d.status = "DRAFT"
	d.target_bank_account = target
	d.keterangan_transfer = keterangan
	d.total_amount = _money(total)
	d.notes = payload.get("notes")
	for idx, i in enumerate(items):
		d.append("items", _disb_item_row(i, idx))
	d.flags.ignore_permissions = True
	d.insert()
	return _disb_read(d.name)


def _disb_update(ident: str, payload: dict) -> dict:
	d = frappe.get_doc("Orion Cash Advance", _disb_name(ident))
	if d.status != "DRAFT":
		frappe.throw("Only DRAFT disbursements can be edited")
	for src, dst in [
		("title", "title"),
		("requestedBy", "requested_by"),
		("preparedBy", "prepared_by"),
		("keteranganTransfer", "keterangan_transfer"),
		("notes", "notes"),
	]:
		if src in payload:
			setattr(d, dst, payload[src])
	if "targetAccountId" in payload:
		target = _resolve_name("Bank Account", payload["targetAccountId"])
		if not target:
			frappe.throw("Target bank account not found", exc=frappe.DoesNotExistError)
		d.target_bank_account = target
	if payload.get("items") is not None:
		d.set("items", [])
		total = Decimal("0")
		for idx, i in enumerate(payload["items"]):
			total += _wdec(i.get("amount"))
			d.append("items", _disb_item_row(i, idx))
		d.total_amount = _money(total)
	d.flags.ignore_permissions = True
	d.save()
	return _disb_read(d.name)


def _disb_delete(ident: str) -> dict:
	d = frappe.get_doc("Orion Cash Advance", _disb_name(ident))
	if d.status != "DRAFT":
		frappe.throw("Only DRAFT disbursements can be deleted")
	frappe.delete_doc("Orion Cash Advance", d.name, force=1, ignore_permissions=True)
	return {"ok": True}


def _disb_transition(ident: str, need: str, to: str, message: str) -> dict:
	d = frappe.get_doc("Orion Cash Advance", _disb_name(ident))
	if d.status != need:
		frappe.throw(message)
	d.status = to
	d.flags.ignore_permissions = True
	d.save()
	return _disb_read(d.name)


def _disb_cancel(ident: str) -> dict:
	d = frappe.get_doc("Orion Cash Advance", _disb_name(ident))
	if d.status not in ("DRAFT", "APPROVED"):
		frappe.throw("Only DRAFT or APPROVED disbursements can be cancelled")
	d.status = "CANCELLED"
	d.flags.ignore_permissions = True
	d.save()
	return _disb_read(d.name)


def _disb_transfer(ident: str, payload: dict) -> dict:
	"""POST /:id/transfer — Dr each item's GL account / Cr the target bank
	account's GL account, then TRANSFERRED + link the CASH_ADVANCE journal entry."""
	d = frappe.get_doc("Orion Cash Advance", _disb_name(ident))
	if d.status != "APPROVED":
		frappe.throw("Only APPROVED disbursements can be transferred")

	transfer_dt = _pdate(payload.get("transferDate")) or frappe.utils.getdate()

	lines = []
	total = Decimal("0")
	for it in d.items:
		acc = _account_by_number(it.gl_account_code) if it.gl_account_code else None
		if not acc:
			fallback = CATEGORY_GL.get(it.category)
			acc = _account_by_number(fallback) if fallback else None
		if not acc:
			frappe.throw(
				"GL account for item '%s' not found. Please set its GL account code."
				% (it.item or it.category or "?")
			)
		amt = _wdec(it.amount)
		total += amt
		lines.append(
			{"account": acc.name, "debit": amt, "credit": Decimal("0"), "description": it.item or it.category}
		)

	target_gl = frappe.db.get_value("Bank Account", d.target_bank_account, "account") if d.target_bank_account else None
	if not target_gl:
		kopra = _account_by_number(KOPRA_GL)
		if not kopra:
			frappe.throw("Funding GL account %s not found" % KOPRA_GL)
		target_gl = kopra.name
	lines.append({"account": target_gl, "debit": Decimal("0"), "credit": total, "description": d.code})

	je = _post_journal_entry(
		posting_date=transfer_dt,
		description=("Cash Advance %s — %s" % (d.code, d.title or "")).strip(),
		reference=d.code,
		source_type="CASH_ADVANCE",
		lines=lines,
		project=d.project,
	)
	d.status = "TRANSFERRED"
	d.transfer_date = transfer_dt
	d.journal_entry = je.name
	d.flags.ignore_permissions = True
	d.save()
	return _disb_read(d.name)
