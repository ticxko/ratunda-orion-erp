"""Step 4f — projects (KN codes become doc.name).

projects.jsonl columns: id, code, name, clientName, client_salutation,
clientPhone, address, businessLine, status, startDate, endDate, budget,
target_margin_pct, description, lctr, kn_sequence, kontrak_yymm,
service_type_code, lead_id, client_id, program_id, kontrak_draft_file.

Status map verified against v16 project.json options
"Open\nOn hold\nCompleted\nCancelled".

Second pass fills Orion Program.ratunda_project / poiesis_project.
"""

import frappe

from orion.migrate import load_jsonl, money, save_map, start_import, to_date

STATUS_MAP = {
	"ACTIVE": "Open",
	"COMPLETED": "Completed",
	"ON_HOLD": "On hold",
	"CANCELLED": "Cancelled",
}
PROGRAM_FIELD = {
	"RATUNDA_RENOVASI": "ratunda_project",
	"POIESIS_STUDIO": "poiesis_project",
}


def run():
	start_import()
	settings = frappe.get_doc("Orion Settings")
	cc_by_line = {
		"RATUNDA_RENOVASI": settings.ratunda_cost_center,
		"POIESIS_STUDIO": settings.poiesis_cost_center,
		"ALL": settings.shared_cost_center,
	}
	program_token = {r["id"]: r["token"] for r in load_jsonl("programs")}

	rows = list(load_jsonl("projects"))
	idmap = {}
	created = 0
	for r in rows:
		existing = frappe.db.get_value("Project", {"orion_legacy_id": r["id"]})
		if existing:
			idmap[r["id"]] = existing
			continue

		doc = frappe.new_doc("Project")
		doc.name = r["code"]
		doc.project_name = r.get("name") or r["code"]
		doc.status = STATUS_MAP.get(r["status"], "Open")
		doc.expected_start_date = to_date(r.get("startDate"))
		doc.expected_end_date = to_date(r.get("endDate"))
		doc.estimated_costing = money(r.get("budget"))
		doc.company = settings.company
		doc.cost_center = cc_by_line.get(r.get("businessLine")) or settings.shared_cost_center
		doc.orion_business_line = r.get("businessLine")
		doc.kn_sequence = r.get("kn_sequence")
		doc.kontrak_yymm = r.get("kontrak_yymm")
		doc.lctr = r.get("lctr")
		doc.orion_service_type = r.get("service_type_code")
		doc.target_margin_pct = r.get("target_margin_pct")
		if r.get("program_id"):
			doc.orion_program = program_token.get(r["program_id"])
		doc.client_salutation = r.get("client_salutation")
		doc.client_phone = r.get("clientPhone")
		doc.client_address = r.get("address")
		doc.orion_legacy_id = r["id"]
		doc.flags.ignore_permissions = True
		doc.insert()
		idmap[r["id"]] = doc.name
		created += 1
		if created % 100 == 0:
			frappe.db.commit()

	save_map("projects", idmap)
	link_programs(rows, idmap, program_token)
	frappe.db.commit()
	print("projects: %s rows (created %s)" % (len(rows), created))


def link_programs(rows, idmap, program_token):
	linked = 0
	for r in rows:
		field = PROGRAM_FIELD.get(r.get("businessLine"))
		token = program_token.get(r.get("program_id"))
		project = idmap.get(r["id"])
		if not (field and token and project):
			continue
		if not frappe.db.exists("Orion Program", token):
			continue
		frappe.db.set_value("Orion Program", token, field, project, update_modified=False)
		linked += 1
	print("projects: linked %s program sides" % linked)
