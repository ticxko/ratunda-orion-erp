"""Step 7a — journal entries (the big one).

journal_entries.jsonl columns (prisma, unmapped -> camelCase): id, date,
description, reference, sourceType, sourceFile, bankAccount, sourceId,
projectId, reversal_of_id, txn_hash.
journal_lines.jsonl columns: id, journalEntryId, accountId, debit, credit,
description.

Orion JEs carry project at header level — it is stamped on every row's
project field. Cost center per row: account business line -> brand CC;
else entry project's brand CC; else Shared.

Entries that do not balance (or have no lines / unmapped accounts) are
skipped and written to migrate-maps/journal_rejects.json for validate.py.

Smoke run:
    bench --site <site> execute orion.migrate.journal_entries.run --kwargs "{'limit': 20}"
"""

import json
import os
from decimal import Decimal

import frappe

from orion.migrate import dec, load_jsonl, load_map, maps_dir, save_map, start_import, to_date

BRAND_LINES = ("RATUNDA_RENOVASI", "POIESIS_STUDIO")


def run(limit=None):
	start_import()
	settings = frappe.get_doc("Orion Settings")
	company = settings.company
	cc_by_line = {
		"RATUNDA_RENOVASI": settings.ratunda_cost_center,
		"POIESIS_STUDIO": settings.poiesis_cost_center,
	}
	shared_cc = settings.shared_cost_center

	accounts_map = load_map("accounts")
	projects_map = load_map("projects")
	account_line = {r["id"]: r.get("businessLine") for r in load_jsonl("accounts")}
	project_line = {r["id"]: r.get("businessLine") for r in load_jsonl("projects")}

	lines_by_entry = {}
	for l in load_jsonl("journal_lines"):
		lines_by_entry.setdefault(l["journalEntryId"], []).append(l)

	entries = list(load_jsonl("journal_entries"))
	je_map = load_map("journal_entries")
	rejects = []
	created = 0
	seen = 0

	for r in entries:
		if limit and seen >= int(limit):
			break
		seen += 1

		existing = frappe.db.get_value("Journal Entry", {"orion_legacy_id": r["id"]})
		if existing:
			je_map[r["id"]] = existing
			continue

		lines = lines_by_entry.get(r["id"]) or []
		problem = check_entry(r, lines, accounts_map)
		if problem:
			rejects.append({"id": r["id"], "date": to_date(r.get("date")), "reason": problem})
			continue

		doc = build_entry(
			r, lines, company, accounts_map, projects_map, account_line, project_line, cc_by_line, shared_cc
		)
		doc.insert()
		doc.submit()
		je_map[r["id"]] = doc.name
		created += 1
		if created % 100 == 0:
			frappe.db.commit()
			print("journal_entries: %s inserted (last %s)" % (created, doc.name), flush=True)

	save_map("journal_entries", je_map)
	write_rejects(rejects)
	link_reversals(entries, je_map)
	frappe.db.commit()
	print(
		"journal_entries: %s created, %s already there, %s rejected (of %s seen)"
		% (created, len(je_map) - created, len(rejects), seen)
	)
	for rej in rejects:
		print("  REJECT %(id)s %(date)s: %(reason)s" % rej)


def check_entry(r, lines, accounts_map):
	if not lines:
		return "no journal lines"
	missing = [l["accountId"] for l in lines if l["accountId"] not in accounts_map]
	if missing:
		return "unmapped accounts: %s" % ", ".join(sorted(set(missing)))
	total_debit = sum(dec(l.get("debit")) for l in lines)
	total_credit = sum(dec(l.get("credit")) for l in lines)
	if total_debit != total_credit:
		return "unbalanced: debit %s != credit %s" % (total_debit, total_credit)
	return None


def build_entry(r, lines, company, accounts_map, projects_map, account_line, project_line, cc_by_line, shared_cc):
	doc = frappe.new_doc("Journal Entry")
	doc.voucher_type = "Journal Entry"
	doc.company = company
	doc.posting_date = to_date(r["date"])
	if r.get("reference"):
		doc.cheque_no = r["reference"]
		doc.cheque_date = doc.posting_date
	doc.user_remark = r.get("description")
	doc.multi_currency = 0
	doc.orion_source_type = r.get("sourceType")
	doc.orion_txn_hash = r.get("txn_hash")
	doc.orion_bank_account_no = r.get("bankAccount")
	doc.orion_source_file = r.get("sourceFile")
	doc.orion_legacy_id = r["id"]

	project = projects_map.get(r["projectId"]) if r.get("projectId") else None
	entry_cc = None
	if r.get("projectId"):
		entry_cc = cc_by_line.get(project_line.get(r["projectId"]))

	two_dp = Decimal("0.01")
	for l in lines:
		acc_bl = account_line.get(l["accountId"])
		if acc_bl in BRAND_LINES:
			cc = cc_by_line[acc_bl]
		elif entry_cc:
			cc = entry_cc
		else:
			cc = shared_cc

		doc.append(
			"accounts",
			{
				"account": accounts_map[l["accountId"]],
				"debit_in_account_currency": float(dec(l.get("debit")).quantize(two_dp)),
				"credit_in_account_currency": float(dec(l.get("credit")).quantize(two_dp)),
				"user_remark": l.get("description"),
				"project": project,
				"cost_center": cc,
			},
		)

	doc.flags.ignore_permissions = True
	return doc


def link_reversals(entries, je_map):
	linked = 0
	for r in entries:
		rev_of = r.get("reversal_of_id")
		if not rev_of:
			continue
		name = je_map.get(r["id"])
		target = je_map.get(rev_of)
		if name and target:
			frappe.db.set_value("Journal Entry", name, "reversal_of", target, update_modified=False)
			linked += 1
	print("journal_entries: linked %s reversals" % linked)


def write_rejects(rejects):
	path = os.path.join(maps_dir(), "journal_rejects.json")
	with open(path, "w") as f:
		json.dump(rejects, f, indent=1, default=str)
