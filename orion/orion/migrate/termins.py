"""Step 4g — project termins.

project_termins.jsonl columns: id, projectId, seq, label, percentage,
planned_amount, expected_date, invoice_id, notes. sales_invoice is left
blank for now (filled when SIs migrate).
"""

import frappe

from orion.migrate import load_jsonl, load_map, money, start_import, to_date


def run():
	start_import()
	projects = load_map("projects")

	created = 0
	total = 0
	for r in load_jsonl("project_termins"):
		total += 1
		if frappe.db.exists("Orion Project Termin", {"orion_legacy_id": r["id"]}):
			continue

		project = projects.get(r["projectId"])
		if not project:
			print("termins: SKIP %s — unmapped project %s" % (r["id"], r["projectId"]))
			continue

		doc = frappe.new_doc("Orion Project Termin")
		doc.project = project
		doc.seq = r["seq"]
		doc.label = r.get("label")
		doc.percentage = float(r["percentage"]) if r.get("percentage") is not None else None
		doc.planned_amount = money(r.get("planned_amount"))
		doc.expected_date = to_date(r.get("expected_date"))
		doc.notes = r.get("notes")
		doc.orion_legacy_id = r["id"]
		doc.flags.ignore_permissions = True
		doc.insert()
		created += 1
		if created % 100 == 0:
			frappe.db.commit()

	frappe.db.commit()
	print("termins: %s rows (created %s)" % (total, created))
