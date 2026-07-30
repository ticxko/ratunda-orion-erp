"""Step 4c — service types (LOV).

service_types.jsonl columns: id, code, label, business_line, is_active.
Orion Service Type autonames from code, so name == code.
"""

import frappe

from orion.migrate import load_jsonl, start_import


def run():
	start_import()
	created = 0
	total = 0
	for r in load_jsonl("service_types"):
		total += 1
		if frappe.db.exists("Orion Service Type", r["code"]):
			continue
		doc = frappe.new_doc("Orion Service Type")
		doc.code = r["code"]
		doc.label = r["label"]
		doc.business_line = r["business_line"]
		doc.is_active = 1 if r.get("is_active", True) else 0
		doc.flags.ignore_permissions = True
		doc.insert()
		created += 1

	frappe.db.commit()
	print("service_types: %s rows (created %s)" % (total, created))
