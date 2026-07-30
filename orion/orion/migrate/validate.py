"""Step 14 — validation gates.

Gate 1 (counts): jsonl line count vs frappe.db.count per importer.
Gate 2 (trial balance): per-account-code sum(debit)/sum(credit) computed
from the JSONL source (Decimal, only entries actually imported — or
replaced by SI/PE, see replaced_je_ids) vs GL Entry aggregates in MariaDB
across ALL voucher types. Zero tolerance.
Gate 3 (accounts receivable): per submitted Sales Invoice, outstanding
must equal source totalAmount minus its GL-producing payments.

Diff CSVs land in sites/<site>/private/migrate-artifacts/.
"""

import csv
import json
import os
from decimal import Decimal

import frappe

from orion.migrate import dec, load_jsonl, maps_dir
from orion.migrate.coa import SPLIT_PARENTS

LEGACY_SET = {"orion_legacy_id": ("is", "set")}

COUNT_GATES = [
	# (jsonl table, doctype, filters; None = count all)
	# split parents add a synthetic grp-* group account — exclude it
	("accounts", "Account", [["orion_legacy_id", "is", "set"], ["orion_legacy_id", "not like", "grp-%"]]),
	("bank_accounts", "Bank Account", LEGACY_SET),
	("users", "User", LEGACY_SET),
	("clients", "Customer", LEGACY_SET),
	("vendors", "Supplier", LEGACY_SET),
	("service_types", "Orion Service Type", None),
	("leads", "Lead", LEGACY_SET),
	("programs", "Orion Program", LEGACY_SET),
	("projects", "Project", LEGACY_SET),
	("project_termins", "Orion Project Termin", LEGACY_SET),
	("journal_entries", "Journal Entry", LEGACY_SET),
]


def run():
	artifacts = frappe.get_site_path("private", "migrate-artifacts")
	os.makedirs(artifacts, exist_ok=True)

	replaced = replaced_je_ids()
	counts_ok = gate_counts(artifacts, replaced)
	tb_ok = gate_trial_balance(artifacts, replaced)
	ar_ok = gate_accounts_receivable(artifacts)

	all_ok = counts_ok and tb_ok and ar_ok
	print("")
	print("validate: gate counts        —", "PASS" if counts_ok else "FAIL")
	print("validate: gate trial balance —", "PASS" if tb_ok else "FAIL")
	print("validate: gate receivables   —", "PASS" if ar_ok else "FAIL")
	print("validate:", "ALL GATES PASS" if all_ok else "GATES FAILED")


def replaced_je_ids():
	"""Source journal entry ids whose GL is now posted by Sales Invoice /
	Payment Entry docs instead of Journal Entry docs (invoices.py, plan §4
	D.4). Derived from what actually exists in the target db — not from the
	maps — so it stays correct after partial or repeated invoice runs:

	- the AR_INVOICE send-JE of every invoice that exists as a submitted
	  Sales Invoice (the send JE is the AR_INVOICE entry NOT referenced by
	  any invoice_payments.journalEntryId — payment JEs share sourceType
	  and sourceId with it);
	- the payment JE of every invoice_payment that exists as a Payment
	  Entry.

	RECLASS payment JEs are re-created as Journal Entry docs under their
	original orion_legacy_id, so they never appear in this set.
	"""
	si_ids = set(
		frappe.get_all(
			"Sales Invoice",
			filters={"orion_legacy_id": ("is", "set"), "docstatus": 1},
			pluck="orion_legacy_id",
		)
	)
	pe_ids = set(frappe.get_all("Payment Entry", filters=LEGACY_SET, pluck="orion_legacy_id"))
	pay_je = {p["id"]: p["journalEntryId"] for p in load_jsonl("invoice_payments") if p.get("journalEntryId")}
	pay_je_ids = set(pay_je.values())

	replaced = {pay_je[pid] for pid in pe_ids if pid in pay_je}
	for r in load_jsonl("journal_entries"):
		if r.get("sourceType") == "AR_INVOICE" and r["id"] not in pay_je_ids and r.get("sourceId") in si_ids:
			replaced.add(r["id"])
	return replaced


def load_rejects():
	path = os.path.join(maps_dir(), "journal_rejects.json")
	if not os.path.exists(path):
		return []
	with open(path) as f:
		return json.load(f)


def gate_counts(artifacts, replaced):
	# journal_entries: a source JE materialises either as a Journal Entry
	# doc, or — AR hybrid, plan §4 D.4 — its GL is posted by the Sales
	# Invoice / Payment Entry that replaced it, or it was rejected at
	# import. `replaced` is computed from the SI/PE docs actually present
	# (replaced_je_ids), so the expectation tracks reality even after a
	# partial invoices run; RECLASS payment JEs are re-created as Journal
	# Entry docs under the same orion_legacy_id and still count as JEs.
	# Union with the reject ids so an id is never subtracted twice.
	skip_ids = {r.get("id") for r in load_rejects()} | replaced
	ok = True
	rows = []
	print("validate: counts")
	for table, doctype, filters in COUNT_GATES:
		expected = sum(1 for _ in load_jsonl(table))
		if table == "journal_entries" and skip_ids:
			expected -= len(skip_ids)
			print("  (journal_entries expectation reduced by %s rejected/replaced entries)" % len(skip_ids))
		actual = frappe.db.count(doctype, filters)
		match = expected == actual
		ok = ok and match
		rows.append([table, doctype, expected, actual, "OK" if match else "DIFF"])
		print(
			"  %-16s %-22s expected %6s  actual %6s  %s"
			% (table, doctype, expected, actual, "OK" if match else "DIFF")
		)

	with open(os.path.join(artifacts, "counts.csv"), "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(["table", "doctype", "expected", "actual", "status"])
		w.writerows(rows)
	return ok


def gate_trial_balance(artifacts, replaced):
	# what actually made it into the ledger: JE docs, plus source entries
	# replaced by SI/PE — their lines stay on the source side because the
	# substitute documents post identical GL (invoices.py preflight)
	imported = set(
		frappe.get_all("Journal Entry", filters=LEGACY_SET, pluck="orion_legacy_id")
	)
	imported |= replaced
	code_of = {r["id"]: r["code"] for r in load_jsonl("accounts")}
	entry_ok = {r["id"] for r in load_jsonl("journal_entries") if r["id"] in imported}

	src = {}
	for l in load_jsonl("journal_lines"):
		if l["journalEntryId"] not in entry_ok:
			continue
		code = code_of[l["accountId"]]
		code = SPLIT_PARENTS.get(code, code)  # posting parents live on their shim leaf
		pair = src.setdefault(code, [Decimal("0"), Decimal("0")])
		pair[0] += dec(l.get("debit"))
		pair[1] += dec(l.get("credit"))

	tgt = {}
	for code, debit, credit in frappe.db.sql(
		"""
		SELECT acc.account_number, SUM(gle.debit), SUM(gle.credit)
		FROM `tabGL Entry` gle
		JOIN `tabAccount` acc ON acc.name = gle.account
		WHERE gle.is_cancelled = 0
		GROUP BY acc.account_number
		"""
	):
		tgt[code] = [dec(debit), dec(credit)]

	diffs = []
	src_dr = src_cr = tgt_dr = tgt_cr = Decimal("0")
	for code in sorted(set(src) | set(tgt)):
		s = src.get(code, [Decimal("0"), Decimal("0")])
		t = tgt.get(code, [Decimal("0"), Decimal("0")])
		src_dr += s[0]
		src_cr += s[1]
		tgt_dr += t[0]
		tgt_cr += t[1]
		if s[0] != t[0] or s[1] != t[1]:
			diffs.append([code, s[0], s[1], t[0], t[1], s[0] - t[0], s[1] - t[1]])

	print("validate: trial balance (all voucher types — SI/PE post GL too)")
	print("  source Dr %s / Cr %s" % (src_dr, src_cr))
	print("  ledger Dr %s / Cr %s" % (tgt_dr, tgt_cr))
	print("  %s account(s) differ" % len(diffs))
	for d in diffs[:20]:
		print("  DIFF %s: src %s/%s vs gl %s/%s" % (d[0], d[1], d[2], d[3], d[4]))
	if len(diffs) > 20:
		print("  ... see trial_balance_diff.csv")

	with open(os.path.join(artifacts, "trial_balance_diff.csv"), "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(["code", "src_debit", "src_credit", "gl_debit", "gl_credit", "diff_debit", "diff_credit"])
		w.writerows(diffs)

	return not diffs and src_dr == tgt_dr and src_cr == tgt_cr


def gate_accounts_receivable(artifacts):
	"""Gate 3 — per submitted Sales Invoice: outstanding_amount must equal
	source totalAmount minus its payments that produced GL (journalEntryId
	set). Payments without a JE never posted GL — neither in Orion nor here
	(invoices.py creates nothing for them) — so they cannot reduce the
	outstanding; they are only counted as a warning."""
	paid = {}
	no_gl = 0
	for p in load_jsonl("invoice_payments"):
		if p.get("journalEntryId"):
			paid[p["invoiceId"]] = paid.get(p["invoiceId"], Decimal("0")) + dec(p.get("amount"))
		else:
			no_gl += 1
	totals = {r["id"]: dec(r.get("totalAmount")) for r in load_jsonl("invoices")}

	two_dp = Decimal("0.01")
	sis = frappe.get_all(
		"Sales Invoice",
		filters={"orion_legacy_id": ("is", "set"), "docstatus": 1},
		fields=["name", "customer", "orion_legacy_id", "outstanding_amount"],
		order_by="name",
	)
	diffs = []
	by_customer = {}
	for si in sis:
		lid = si.orion_legacy_id
		expected = (totals.get(lid, Decimal("0")) - paid.get(lid, Decimal("0"))).quantize(two_dp)
		actual = dec(si.outstanding_amount).quantize(two_dp)
		pair = by_customer.setdefault(si.customer, [Decimal("0"), Decimal("0")])
		pair[0] += expected
		pair[1] += actual
		if expected != actual:
			diffs.append([si.name, lid, si.customer, expected, actual, expected - actual])

	print("validate: accounts receivable (%s submitted Sales Invoices)" % len(sis))
	if no_gl:
		print(
			"  WARNING %s source payment(s) have no journal entry — no GL exists"
			" for them anywhere, excluded from expected outstanding" % no_gl
		)
	print("  outstanding per customer (expected / actual):")
	for cust in sorted(by_customer):
		e, a = by_customer[cust]
		print("    %-45s %18s %18s%s" % (cust, e, a, "" if e == a else "  DIFF"))
	print("  %s invoice(s) differ" % len(diffs))
	for d in diffs[:20]:
		print("  DIFF %s (%s): expected %s actual %s" % (d[0], d[2], d[3], d[4]))
	if len(diffs) > 20:
		print("  ... see ar_diff.csv")

	with open(os.path.join(artifacts, "ar_diff.csv"), "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(["sales_invoice", "legacy_id", "customer", "expected_outstanding", "actual_outstanding", "diff"])
		w.writerows(diffs)
	return not diffs
