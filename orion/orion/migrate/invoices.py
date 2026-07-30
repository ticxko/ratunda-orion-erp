"""Step 7b — Sales Invoices + Payment Entries (the AR hybrid, plan §4 D.4).

Orion posts invoice GL through Journal Entries (see the source router
app/routers/accounting/invoices.py): sending an invoice posts
Dr AR / Cr revenue with sourceType AR_INVOICE and sourceId = invoice id;
recording a payment (post_invoice_payment) posts Dr funding account / Cr AR,
where the funding account is a bank account or — via the advance
reconciliation engine — the client-advance liability 2-1210/2-1220.
NOTE: payment JEs carry the SAME sourceType/sourceId as the send JE; the
send JE is the one NOT referenced by any invoice_payments.journalEntryId.

This step replaces those already-imported JEs with real Sales Invoices and
Payment Entries posting byte-identical GL:

  invoice -> Sales Invoice  debit_to = the send JE's single Dr account, one
                            item per Cr line (income_account = that account,
                            rate = its credit) — GL-exact regardless of
                            invoice_items.
  payment -> Payment Entry  when its JE is exactly Dr X / Cr debit_to with
                            both amounts == the payment amount,
          -> reclass JE     otherwise: the original JE re-created verbatim,
                            its Cr line on the AR account referencing the
                            Sales Invoice so it allocates against it.

Invoices whose AR JE deviates from the expected shape keep their JEs
untouched and are recorded (with payments that never had a JE, i.e. never
had GL) in migrate-maps/invoice_rejects.json for validate.py.

Smoke run:
    bench --site <site> execute orion.migrate.invoices.run --kwargs "{'limit': 5}"
"""

import json
import os

import frappe

from orion.migrate import dec, load_jsonl, load_map, maps_dir, money, save_map, start_import, to_date
from orion.migrate.customers import ensure_customer_group, ensure_territory
from orion.migrate.journal_entries import build_entry


def run(limit=None):
	start_import()
	settings = frappe.get_doc("Orion Settings")
	ctx = {
		"company": settings.company,
		"cc_by_line": {
			"RATUNDA_RENOVASI": settings.ratunda_cost_center,
			"POIESIS_STUDIO": settings.poiesis_cost_center,
		},
		"shared_cc": settings.shared_cost_center,
		"accounts_map": load_map("accounts"),
		"accounts_by_code": load_map("accounts_by_code"),
		"customers_map": load_map("customers"),
		"projects_map": load_map("projects"),
	}
	acct_rows = list(load_jsonl("accounts"))
	ctx["account_line"] = {r["id"]: r.get("businessLine") for r in acct_rows}
	ctx["account_name"] = {r["id"]: r.get("name") for r in acct_rows}
	ctx["account_type"] = {r["id"]: r.get("type") for r in acct_rows}
	ctx["project_line"] = {r["id"]: r.get("businessLine") for r in load_jsonl("projects")}

	lines_by_entry = {}
	for l in load_jsonl("journal_lines"):
		lines_by_entry.setdefault(l["journalEntryId"], []).append(l)
	entries = {r["id"]: r for r in load_jsonl("journal_entries")}

	items_by_inv = {}
	for it in load_jsonl("invoice_items"):
		items_by_inv.setdefault(it["invoiceId"], []).append(it)
	for its in items_by_inv.values():
		its.sort(key=lambda it: (it.get("sortOrder") or 0, it.get("createdAt") or ""))

	pays_by_inv = {}
	pay_je_ids = set()
	for p in load_jsonl("invoice_payments"):
		pays_by_inv.setdefault(p["invoiceId"], []).append(p)
		if p.get("journalEntryId"):
			pay_je_ids.add(p["journalEntryId"])
	for ps in pays_by_inv.values():
		ps.sort(key=lambda p: (to_date(p.get("date")) or "", p.get("createdAt") or ""))

	# the invoice-send JE is the AR_INVOICE entry for this sourceId that no
	# invoice_payment points at (payment JEs share sourceType and sourceId)
	ar_jes = {}
	for r in entries.values():
		if r.get("sourceType") == "AR_INVOICE" and r["id"] not in pay_je_ids and r.get("sourceId"):
			ar_jes.setdefault(r["sourceId"], []).append(r)

	# ── preflight: pure, no writes ──────────────────────────────────────
	plans = []
	rejects = []
	payments_without_gl = []
	for r in load_jsonl("invoices"):
		jes = ar_jes.get(r["id"]) or []
		inv_items = items_by_inv.get(r["id"]) or []
		pays = pays_by_inv.get(r["id"]) or []
		if len(jes) > 1:
			plan, problem, no_gl = None, "%s AR_INVOICE journal entries (want 1)" % len(jes), []
		elif jes:
			plan, problem, no_gl = plan_submitted(r, jes[0], entries, lines_by_entry, inv_items, pays, ctx)
		elif r.get("status") == "DRAFT":
			plan, problem, no_gl = plan_draft(r, inv_items, pays, ctx)
		else:
			plan, problem, no_gl = None, "no AR_INVOICE journal entry (status %s)" % r.get("status"), []
		if problem:
			rejects.append({"id": r["id"], "number": r.get("number"), "status": r.get("status"), "reason": problem})
		else:
			plans.append(plan)
			payments_without_gl.extend(no_gl)

	receivables = set_receivable_types(plans)
	ensure_round_off_account(ctx["company"], ctx["shared_cc"])

	# ── execute ─────────────────────────────────────────────────────────
	je_map = load_map("journal_entries")
	inv_map = load_map("invoices")
	pay_map = load_map("invoice_payments")
	cache = {}
	counts = {"si": 0, "draft": 0, "skip": 0, "pe": 0, "reclass": 0, "dropped": 0}
	done = 0

	for plan in plans:
		if limit and done >= int(limit):
			break
		r = plan["invoice"]
		existing = frappe.db.get_value("Sales Invoice", {"orion_legacy_id": r["id"]})
		if existing:
			inv_map[r["id"]] = existing
			counts["skip"] += 1
			continue
		done += 1

		# a. the JEs this invoice replaces go away first (GL moves to SI/PE)
		for jeid in plan["replaced"]:
			drop_journal_entry(jeid, je_map)
			counts["dropped"] += 1

		# b. the Sales Invoice
		customer = resolve_customer(r, ctx["customers_map"], cache)
		si = build_invoice(plan, customer, ctx)
		si.insert()
		if plan["kind"] == "submitted":
			si.submit()
			counts["si"] += 1
		else:
			counts["draft"] += 1
		inv_map[r["id"]] = si.name

		# c. its payments, in date order
		for pp in plan["payments"]:
			if pp["kind"] == "PE":
				pe = build_payment_entry(pp, plan, si.name, customer, ctx)
				pe.insert()
				pe.submit()
				pay_map[pp["payment"]["id"]] = pe.name
				counts["pe"] += 1
			else:
				je = rebuild_reclass(pp, plan, si.name, customer, receivables, ctx)
				je.insert()
				je.submit()
				je_map[pp["je"]["id"]] = je.name
				counts["reclass"] += 1

		if done % 25 == 0:
			frappe.db.commit()
			print("invoices: %s processed (last %s)" % (done, si.name), flush=True)

	save_map("invoices", inv_map)
	save_map("invoice_payments", pay_map)
	save_map("journal_entries", je_map)
	write_report(rejects, payments_without_gl)
	frappe.db.commit()

	print(
		"invoices: %(si)s SI submitted, %(draft)s SI draft, %(skip)s already there, "
		"%(pe)s payment entries, %(reclass)s reclass JEs, %(dropped)s source JEs replaced" % counts
	)
	print(
		"invoices: %s rejected (JEs kept), %s payments without GL (nothing created)"
		% (len(rejects), len(payments_without_gl))
	)
	for rej in rejects:
		print("  REJECT %(id)s %(number)s [%(status)s]: %(reason)s" % rej)


# ── preflight helpers ───────────────────────────────────────────────────────


def plan_submitted(r, je, entries, lines_by_entry, inv_items, pays, ctx):
	"""Plan an SI that reproduces the AR JE's GL exactly: its single Dr line
	becomes debit_to, every Cr line becomes one item (income_account, rate).
	Returns (plan, problem, payments_without_gl_rows)."""
	accounts_map = ctx["accounts_map"]
	lines = lines_by_entry.get(je["id"]) or []
	if not lines:
		return None, "AR JE %s has no lines" % je["id"], []
	if any(dec(l.get("debit")) > 0 and dec(l.get("credit")) > 0 for l in lines):
		return None, "AR JE %s has a line with both debit and credit" % je["id"], []
	dr = [l for l in lines if dec(l.get("debit")) > 0]
	cr = [l for l in lines if dec(l.get("credit")) > 0]
	if len(dr) != 1:
		return None, "AR JE %s has %s debit lines (want 1)" % (je["id"], len(dr)), []
	if not cr:
		return None, "AR JE %s has no credit lines" % je["id"], []
	missing = sorted({l["accountId"] for l in lines if l["accountId"] not in accounts_map})
	if missing:
		return None, "AR JE %s unmapped accounts: %s" % (je["id"], ", ".join(missing)), []
	if dec(dr[0].get("debit")) != sum(dec(l.get("credit")) for l in cr):
		return None, "AR JE %s debit != sum of credits" % je["id"], []

	# an advance-application JE (Dr 2-12xx liability / Cr AR) shares
	# sourceType+sourceId with the send JE — never let it pose as one, or
	# a liability account would get flipped to Receivable
	if ctx["account_type"].get(dr[0]["accountId"]) != "ASSET":
		return None, (
			"AR JE %s debit account is %s, not ASSET — advance-application JE misidentified as send JE?"
			% (je["id"], ctx["account_type"].get(dr[0]["accountId"]))
		), []

	debit_to = accounts_map[dr[0]["accountId"]]
	posting = to_date(je.get("date"))
	due = to_date(r.get("dueDate")) or posting
	if due < posting:
		due = posting  # ERPNext forbids due date before posting date

	cc = ctx["cc_by_line"].get(r.get("businessLine")) or ctx["shared_cc"]
	paired = inv_items if len(inv_items) == len(cr) else None
	items = []
	for i, l in enumerate(cr):
		label = (paired[i].get("description") or "").strip() if paired else ""
		if not label:
			label = ctx["account_name"].get(l["accountId"]) or "Invoice item"
		items.append({
			"item_name": label[:140],
			"description": label,
			"income_account": accounts_map[l["accountId"]],
			"rate": dec(l.get("credit")),
			"cost_center": cc,
			"percentage": paired[i].get("percentage") if paired else None,
		})

	payments = []
	no_gl = []
	replaced = [je["id"]]
	for p in pays:
		jeid = p.get("journalEntryId")
		if not jeid:
			no_gl.append(no_gl_row(r, p))
			continue
		plines = lines_by_entry.get(jeid) or []
		if jeid not in entries or not plines:
			return None, "payment %s: journal entry %s not in source" % (p["id"], jeid), []
		missing = sorted({l["accountId"] for l in plines if l["accountId"] not in accounts_map})
		if missing:
			return None, "payment JE %s unmapped accounts: %s" % (jeid, ", ".join(missing)), []
		payments.append(classify_payment(p, entries[jeid], plines, debit_to, ctx))
		replaced.append(jeid)

	plan = {
		"invoice": r,
		"kind": "submitted",
		"debit_to": debit_to,
		"posting_date": posting,
		"due_date": due,
		"items": items,
		"payments": payments,
		"replaced": replaced,
	}
	return plan, None, no_gl


def classify_payment(p, je, lines, debit_to, ctx):
	"""PE-COMPATIBLE: exactly two lines, Dr X / Cr <invoice debit_to>, both
	equal to the payment amount. Anything else is re-created verbatim as a
	Journal Entry (RECLASS) whose Cr-AR line allocates against the SI."""
	amount = dec(p.get("amount"))
	if len(lines) == 2:
		dr = [l for l in lines if dec(l.get("debit")) > 0 and dec(l.get("credit")) == 0]
		cr = [l for l in lines if dec(l.get("credit")) > 0 and dec(l.get("debit")) == 0]
		if (
			len(dr) == 1
			and len(cr) == 1
			and ctx["accounts_map"][cr[0]["accountId"]] == debit_to
			and dec(dr[0].get("debit")) == amount
			and dec(cr[0].get("credit")) == amount
		):
			return {"kind": "PE", "payment": p, "je": je, "paid_to": ctx["accounts_map"][dr[0]["accountId"]]}
	return {"kind": "RECLASS", "payment": p, "je": je, "lines": lines}


def plan_draft(r, inv_items, pays, ctx):
	"""Unsent invoice: no GL exists, so a draft SI (never submitted).
	Mirrors send_invoice(): revenue = revenueAccountId else the brand default
	(4-2100 Poiesis / 4-1100 Ratunda); AR = 1-1220 Poiesis / 1-1210 Ratunda."""
	if any(p.get("journalEntryId") for p in pays):
		return None, "DRAFT invoice has GL-bearing payments", []
	if not inv_items:
		return None, "DRAFT invoice has no items", []

	accounts_map = ctx["accounts_map"]
	by_code = ctx["accounts_by_code"]
	poiesis = r.get("businessLine") == "POIESIS_STUDIO"
	if r.get("revenueAccountId"):
		rev = accounts_map.get(r["revenueAccountId"])
		if not rev:
			return None, "unmapped revenue account %s" % r["revenueAccountId"], []
	else:
		code = "4-2100" if poiesis else "4-1100"
		rev = by_code.get(code)
		if not rev:
			return None, "default revenue account %s not found" % code, []
	ar_code = "1-1220" if poiesis else "1-1210"
	debit_to = by_code.get(ar_code)
	if not debit_to:
		return None, "AR account %s not found" % ar_code, []

	posting = to_date(r.get("issueDate"))
	due = to_date(r.get("dueDate")) or posting
	if due < posting:
		due = posting

	cc = ctx["cc_by_line"].get(r.get("businessLine")) or ctx["shared_cc"]
	items = []
	for it in inv_items:
		label = (it.get("description") or "").strip() or "Invoice item"
		items.append({
			"item_name": label[:140],
			"description": label,
			"income_account": rev,
			"rate": dec(it.get("amount")),
			"cost_center": cc,
			"percentage": it.get("percentage"),
		})

	plan = {
		"invoice": r,
		"kind": "draft",
		"debit_to": debit_to,
		"posting_date": posting,
		"due_date": due,
		"items": items,
		"payments": [],
		"replaced": [],
	}
	return plan, None, [no_gl_row(r, p) for p in pays]


def no_gl_row(r, p):
	return {
		"payment_id": p["id"],
		"invoice_id": r["id"],
		"invoice": r.get("number"),
		"date": to_date(p.get("date")),
		"amount": p.get("amount"),
	}


# ── execute helpers ─────────────────────────────────────────────────────────


def ensure_round_off_account(company, shared_cc):
	"""SI submit validates Company round_off_account even when nothing needs
	rounding (source amounts are exact 2dp). Orion's CoA has no such account,
	so create an inert 8-9990 Pembulatan leaf under the other-expense tree."""
	if frappe.db.get_value("Company", company, "round_off_account"):
		return
	acc = frappe.db.get_value("Account", {"company": company, "account_number": "8-9990"})
	if not acc:
		parent = frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 1, "account_number": ("like", "8%")},
			order_by="account_number",
		) or frappe.db.get_value(
			"Account", {"company": company, "is_group": 1, "root_type": "Expense", "parent_account": ("is", "not set")}
		)
		doc = frappe.new_doc("Account")
		doc.company = company
		doc.account_name = "Pembulatan"
		doc.account_number = "8-9990"
		doc.parent_account = parent
		doc.root_type = "Expense"
		doc.report_type = "Profit and Loss"
		doc.account_type = "Round Off"
		doc.flags.ignore_permissions = True
		doc.insert()
		acc = doc.name
		print("invoices: created round-off account", acc)
	frappe.db.set_value("Company", company, "round_off_account", acc, update_modified=False)
	frappe.db.set_value("Company", company, "round_off_cost_center", shared_cc, update_modified=False)
	frappe.db.commit()


def set_receivable_types(plans):
	"""Every debit_to must be a Receivable account before SI insert (SI
	validates it; GL/PE demand a party on it afterwards)."""
	accounts = sorted({pl["debit_to"] for pl in plans})
	flipped = 0
	for name in accounts:
		if frappe.db.get_value("Account", name, "account_type") != "Receivable":
			frappe.db.set_value("Account", name, "account_type", "Receivable")
			flipped += 1
	if flipped:
		frappe.db.commit()
		print("invoices: set account_type=Receivable on %s account(s)" % flipped)
	return set(accounts)


def drop_journal_entry(legacy_id, je_map):
	je_map.pop(legacy_id, None)
	name = frappe.db.get_value("Journal Entry", {"orion_legacy_id": legacy_id})
	if not name:
		print("invoices: WARNING replaced JE %s has no Journal Entry doc" % legacy_id)
		return
	je = frappe.get_doc("Journal Entry", name)
	je.flags.ignore_permissions = True
	if je.docstatus == 1:
		je.cancel()
	frappe.delete_doc("Journal Entry", name, force=1, ignore_permissions=True)


def resolve_customer(r, customers_map, cache):
	if r.get("clientId") and customers_map.get(r["clientId"]):
		return customers_map[r["clientId"]]
	name = (r.get("clientName") or "").strip() or "Unknown Client"
	if name in cache:
		return cache[name]
	found = frappe.db.get_value("Customer", {"customer_name": name})
	if found:
		cache[name] = found
		return found
	if "_group" not in cache:
		cache["_group"] = ensure_customer_group()
		cache["_territory"] = ensure_territory()
	doc = frappe.new_doc("Customer")
	doc.customer_name = name
	doc.customer_type = "Individual"
	doc.customer_group = cache["_group"]
	doc.territory = cache["_territory"]
	doc.flags.ignore_permissions = True
	doc.insert()
	cache[name] = doc.name
	return doc.name


def build_invoice(plan, customer, ctx):
	r = plan["invoice"]
	doc = frappe.new_doc("Sales Invoice")
	doc.name = r["number"]  # start_import()'s in_import flag makes preset names stick
	doc.customer = customer
	doc.company = ctx["company"]
	doc.currency = "IDR"
	doc.conversion_rate = 1
	doc.set_posting_time = 1
	doc.posting_date = plan["posting_date"]
	doc.due_date = plan["due_date"]
	doc.debit_to = plan["debit_to"]
	doc.update_stock = 0
	doc.disable_rounded_total = 1
	doc.project = ctx["projects_map"].get(r["projectId"]) if r.get("projectId") else None
	doc.orion_legacy_id = r["id"]
	for it in plan["items"]:
		doc.append(
			"items",
			{
				"item_name": it["item_name"],
				"description": it["description"],
				"qty": 1,
				"uom": "Nos",
				"rate": money(it["rate"]),
				"income_account": it["income_account"],
				"cost_center": it["cost_center"],
				"project": doc.project,
				"orion_percentage": money(it["percentage"]) if it.get("percentage") is not None else None,
			},
		)
	doc.flags.ignore_permissions = True
	return doc


def build_payment_entry(pp, plan, si_name, customer, ctx):
	p = pp["payment"]
	posting = to_date(pp["je"].get("date")) or to_date(p.get("date"))
	total, outstanding = frappe.db.get_value(
		"Sales Invoice", si_name, ["grand_total", "outstanding_amount"]
	)
	amount = dec(p.get("amount"))
	# clamp: source tolerated 1-cent overpay; the excess stays as an
	# unallocated advance on the same AR account — GL is unchanged
	allocated = min(amount, dec(outstanding))

	doc = frappe.new_doc("Payment Entry")
	doc.payment_type = "Receive"
	doc.company = ctx["company"]
	doc.posting_date = posting
	doc.party_type = "Customer"
	doc.party = customer
	doc.paid_from = plan["debit_to"]
	doc.paid_to = pp["paid_to"]
	doc.paid_from_account_currency = "IDR"
	doc.paid_to_account_currency = "IDR"
	doc.source_exchange_rate = 1
	doc.target_exchange_rate = 1
	doc.paid_amount = money(amount)
	doc.received_amount = money(amount)
	doc.reference_no = p.get("reference") or p.get("receiptCode") or "-"
	doc.reference_date = posting
	doc.cost_center = ctx["shared_cc"]
	doc.orion_receipt_code = p.get("receiptCode")
	doc.orion_legacy_id = p["id"]
	doc.append(
		"references",
		{
			"reference_doctype": "Sales Invoice",
			"reference_name": si_name,
			"total_amount": total,
			"outstanding_amount": outstanding,
			"allocated_amount": money(allocated),
		},
	)
	doc.flags.ignore_permissions = True
	return doc


def rebuild_reclass(pp, plan, si_name, customer, receivables, ctx):
	"""Re-create the original payment JE verbatim (same custom fields, same
	orion_legacy_id/txn hash via build_entry), except the Cr line on the AR
	account now references the Sales Invoice so it allocates against it.
	Lines on any (now Receivable) AR account get the SI's customer as party
	— GL Entry demands a party on Receivable accounts."""
	doc = build_entry(
		pp["je"],
		pp["lines"],
		ctx["company"],
		ctx["accounts_map"],
		ctx["projects_map"],
		ctx["account_line"],
		ctx["project_line"],
		ctx["cc_by_line"],
		ctx["shared_cc"],
	)
	for row in doc.accounts:
		if row.account in receivables:
			row.party_type = "Customer"
			row.party = customer
		if row.account == plan["debit_to"] and dec(row.credit_in_account_currency) > 0:
			row.reference_type = "Sales Invoice"
			row.reference_name = si_name
	return doc


def write_report(rejects, payments_without_gl):
	path = os.path.join(maps_dir(), "invoice_rejects.json")
	with open(path, "w") as f:
		json.dump(
			{"invoice_rejects": rejects, "payments_without_gl": payments_without_gl},
			f,
			indent=1,
			default=str,
		)
