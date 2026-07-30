"""Step 4e — programs.

programs.jsonl columns: id, token, name, client_id, site_address, status.
Orion Program autonames from token (name == token). ratunda_project /
poiesis_project are filled by projects.py's second pass.
"""

import frappe

from orion.migrate import load_jsonl, load_map, start_import


def run():
	start_import()
	customers = load_map("customers")
	created = 0
	total = 0
	for r in load_jsonl("programs"):
		total += 1
		if frappe.db.exists("Orion Program", {"orion_legacy_id": r["id"]}):
			continue

		doc = frappe.new_doc("Orion Program")
		doc.token = r["token"]
		doc.program_name = r["name"]
		if r.get("client_id"):
			doc.client = customers.get(r["client_id"])
		doc.site_address = r.get("site_address")
		doc.status = r["status"]
		doc.orion_legacy_id = r["id"]
		doc.flags.ignore_permissions = True
		doc.insert()
		created += 1

	frappe.db.commit()
	print("programs: %s rows (created %s)" % (total, created))
