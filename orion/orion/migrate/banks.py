"""Step 2 — banks + bank accounts.

bank_accounts.jsonl columns (prisma, unmapped -> camelCase): id, bankName,
accountNumber, accountHolderName, ownerType, accountId, vendorId,
employeeId, businessLine, purpose, currency, isActive, notes.

party_type/party linkage is intentionally skipped for now — vendors and
employees may not exist yet at this point of the pipeline.
"""

import frappe

from orion.migrate import load_jsonl, load_map, save_map, start_import


def run():
	start_import()
	company = frappe.get_doc("Orion Settings").company
	accounts_map = load_map("accounts")

	idmap = {}
	created = 0
	for r in load_jsonl("bank_accounts"):
		existing = frappe.db.get_value("Bank Account", {"orion_legacy_id": r["id"]})
		if existing:
			idmap[r["id"]] = existing
			continue

		bank = ensure_bank(r["bankName"])

		doc = frappe.new_doc("Bank Account")
		doc.account_name = unique_account_name(r["accountHolderName"], bank, r["accountNumber"])
		doc.bank = bank
		doc.bank_account_no = r["accountNumber"]
		gl = accounts_map.get(r["accountId"]) if r.get("accountId") else None
		# v16 makes `account` ("Company Account") mandatory once
		# is_company_account is set — only flag rows that carry a GL link
		if r["ownerType"] == "COMPANY" and gl:
			doc.is_company_account = 1
			doc.company = company
		if gl:
			doc.account = gl
		doc.disabled = 0 if r.get("isActive", True) else 1
		doc.orion_owner_type = r["ownerType"]
		doc.orion_purpose = r.get("purpose")
		doc.orion_legacy_id = r["id"]
		doc.flags.ignore_permissions = True
		doc.insert()
		idmap[r["id"]] = doc.name
		created += 1
		if created % 100 == 0:
			frappe.db.commit()

	save_map("bank_accounts", idmap)
	frappe.db.commit()
	print("banks: %s bank accounts (created %s)" % (len(idmap), created))


def ensure_bank(bank_name):
	name = frappe.db.get_value("Bank", {"bank_name": bank_name})
	if name:
		return name
	doc = frappe.new_doc("Bank")
	doc.bank_name = bank_name
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def unique_account_name(holder, bank, account_number):
	"""Bank Account names itself '{account_name} - {bank}' — disambiguate
	a second account of the same holder at the same bank."""
	if frappe.db.exists("Bank Account", "%s - %s" % (holder, bank)):
		return "%s %s" % (holder, account_number)
	return holder
