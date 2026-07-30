"""Step 14 — validation gates.

Gate 1 (counts): jsonl line count vs frappe.db.count per importer.
Gate 2 (trial balance): per-account-code sum(debit)/sum(credit) computed
from the JSONL source (Decimal, only entries actually imported) vs GL
Entry aggregates in MariaDB. Zero tolerance.

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

	counts_ok = gate_counts(artifacts)
	tb_ok = gate_trial_balance(artifacts)

	print("")
	print("validate: gate counts        —", "PASS" if counts_ok else "FAIL")
	print("validate: gate trial balance —", "PASS" if tb_ok else "FAIL")
	print("validate:", "ALL GATES PASS" if (counts_ok and tb_ok) else "GATES FAILED")


def load_rejects():
	path = os.path.join(maps_dir(), "journal_rejects.json")
	if not os.path.exists(path):
		return []
	with open(path) as f:
		return json.load(f)


def gate_counts(artifacts):
	rejects = len(load_rejects())
	ok = True
	rows = []
	print("validate: counts")
	for table, doctype, filters in COUNT_GATES:
		expected = sum(1 for _ in load_jsonl(table))
		if table == "journal_entries" and rejects:
			expected -= rejects
			print("  (journal_entries expectation reduced by %s rejects)" % rejects)
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


def gate_trial_balance(artifacts):
	# what actually made it into the ledger
	imported = set(
		frappe.get_all("Journal Entry", filters=LEGACY_SET, pluck="orion_legacy_id")
	)
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
		WHERE gle.is_cancelled = 0 AND gle.voucher_type = 'Journal Entry'
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

	print("validate: trial balance (Journal Entry vouchers only)")
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
