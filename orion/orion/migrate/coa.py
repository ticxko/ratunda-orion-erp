"""Step 1 — chart of accounts.

Replaces the wizard's standard chart with Orion's 86 accounts from
accounts.jsonl (columns: id, code, name, type, businessLine, parentId,
isHeader, isBank, isCash, isActive, ...).

unset_existing_data(company) verified against erpnext v16
chart_of_accounts_importer.py — deletes Account, Party Account, Mode of
Payment Account, Tax Withholding Account and both tax templates for the
company, and blanks Account-linked Company fields.
"""

import frappe

from orion.migrate import load_jsonl, save_map, start_import

ROOT_TYPE = {
	"ASSET": "Asset",
	"LIABILITY": "Liability",
	"EQUITY": "Equity",
	"REVENUE": "Income",
	"OTHER_INCOME": "Income",
	"COGS": "Expense",
	"EXPENSE": "Expense",
	"OTHER_EXPENSE": "Expense",
}
REPORT_TYPE = {
	"Asset": "Balance Sheet",
	"Liability": "Balance Sheet",
	"Equity": "Balance Sheet",
	"Income": "Profit and Loss",
	"Expense": "Profit and Loss",
}

# Orion allows posting to an account that also has children; ERPNext does not.
# Declared source parents become a group (original code) plus a posting shim
# leaf (suffix .1) that inherits their journal lines. Source context: 6-1600
# still carries 11 direct lines from before its Ratunda(6-1601)/Poiesis(6-1602)
# mirror split was finished — reclassify in the pre-cutover cleanup review.
SPLIT_PARENTS = {"6-1600": "6-1600.1"}


def run():
	start_import()
	company = frappe.get_doc("Orion Settings").company
	if not company:
		frappe.throw("Orion Settings has no company — run orion.migrate.company first")

	rows = list(load_jsonl("accounts"))
	wipe_standard_chart(company)

	has_children = {r["parentId"] for r in rows if r.get("parentId")}
	undeclared = [
		r["code"]
		for r in rows
		if r["id"] in has_children and not r.get("isHeader") and r["code"] not in SPLIT_PARENTS
	]
	if undeclared:
		frappe.throw("coa: posting accounts with children need a SPLIT_PARENTS entry: %s" % undeclared)

	idmap = {}  # legacy id -> posting account (shim leaf for split parents)
	parent_map = {}  # legacy id -> account usable as parent (group for split parents)
	by_code = {}

	for r in ordered(rows):
		if r["id"] in has_children and not r.get("isHeader"):
			group, leaf = insert_split_parent(r, company, parent_map)
			idmap[r["id"]] = leaf
			parent_map[r["id"]] = group
			by_code[r["code"]] = group
			by_code[SPLIT_PARENTS[r["code"]]] = leaf
		else:
			name = insert_account(r, company, parent_map)
			idmap[r["id"]] = name
			parent_map[r["id"]] = name
			by_code[r["code"]] = name

	save_map("accounts", idmap)
	save_map("accounts_by_code", by_code)
	set_company_defaults(company)
	frappe.db.commit()
	print("coa: %s accounts in place (%s source rows)" % (len(idmap), len(rows)))


def wipe_standard_chart(company):
	if frappe.db.exists("Account", {"company": company, "orion_legacy_id": ("is", "set")}):
		return  # Orion chart already (partially) in — never wipe again
	if frappe.db.count("GL Entry", {"company": company}):
		frappe.throw("GL Entries exist for %s — refusing to wipe chart" % company)

	from erpnext.accounts.doctype.chart_of_accounts_importer.chart_of_accounts_importer import (
		unset_existing_data,
	)

	unset_existing_data(company)
	print("coa: wiped standard chart for", company)


def ordered(rows):
	"""Parents before children (topological walk from the roots)."""
	children = {}
	roots = []
	for r in rows:
		if r.get("parentId"):
			children.setdefault(r["parentId"], []).append(r)
		else:
			roots.append(r)

	out = []
	stack = sorted(roots, key=lambda r: r["code"], reverse=True)
	while stack:
		r = stack.pop()
		out.append(r)
		stack.extend(sorted(children.get(r["id"], []), key=lambda x: x["code"], reverse=True))
	if len(out) != len(rows):
		frappe.throw("coa: orphan accounts detected (%s of %s reachable)" % (len(out), len(rows)))
	return out


def insert_account(r, company, idmap):
	existing = frappe.db.get_value("Account", {"orion_legacy_id": r["id"]})
	if existing:
		return existing

	root_type = ROOT_TYPE[r["type"]]
	doc = frappe.new_doc("Account")
	doc.company = company
	doc.account_name = r["name"]
	doc.account_number = r["code"]
	doc.is_group = 1 if r.get("isHeader") else 0
	doc.root_type = root_type
	doc.report_type = REPORT_TYPE[root_type]
	if r.get("parentId"):
		doc.parent_account = idmap[r["parentId"]]
	else:
		doc.flags.ignore_mandatory = True  # root accounts have no parent

	if r.get("isBank"):
		doc.account_type = "Bank"
	elif r.get("isCash"):
		doc.account_type = "Cash"
	elif r["type"] == "COGS" and not r.get("isHeader"):
		doc.account_type = "Cost of Goods Sold"

	doc.disabled = 0 if r.get("isActive", True) else 1
	doc.orion_business_line = r.get("businessLine")
	doc.orion_legacy_id = r["id"]
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def insert_split_parent(r, company, parent_map):
	"""Group with the original code + posting shim leaf under it."""
	group = frappe.db.get_value("Account", {"orion_legacy_id": "grp-%s" % r["id"]})
	if not group:
		g = dict(r, isHeader=True, id="grp-%s" % r["id"], isBank=False, isCash=False)
		group = insert_account(g, company, parent_map)

	leaf = frappe.db.get_value("Account", {"orion_legacy_id": r["id"]})
	if not leaf:
		grp_id = "grp-%s" % r["id"]
		l = dict(r, isHeader=False, code=SPLIT_PARENTS[r["code"]], parentId=grp_id)
		leaf = insert_account(l, company, {**parent_map, grp_id: group})
	return group, leaf


def set_company_defaults(company):
	"""Best-effort defaults; SI/PE importers set accounts explicitly anyway."""
	shared_cc = frappe.get_doc("Orion Settings").shared_cost_center

	round_off = find_leaf(company, ["Pembulatan", "Round Off", "Rounding"])
	if round_off:
		frappe.db.set_value("Company", company, "round_off_account", round_off, update_modified=False)
		if shared_cc:
			frappe.db.set_value(
				"Company", company, "round_off_cost_center", shared_cc, update_modified=False
			)

	write_off = find_leaf(company, ["Write Off", "Penghapusan"])
	if write_off:
		frappe.db.set_value("Company", company, "write_off_account", write_off, update_modified=False)


def find_leaf(company, keywords):
	for kw in keywords:
		name = frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 0, "account_name": ("like", "%%%s%%" % kw)},
		)
		if name:
			return name
	return None
