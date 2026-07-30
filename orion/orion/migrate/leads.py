"""Step 4d — leads.

leads.jsonl columns (@map'd): id, lead_code, short_name, client_name,
client_salutation, client_phone, client_address, business_line,
service_interest, estimated_value, source, status, assigned_to, notes,
client_id.

doc.name is preset to lead_code (survives autoname because
frappe.flags.in_import is set). Raw Orion status is kept in
orion_lead_status; mapped to the closest stock Lead status (v16 options:
Lead/Open/Replied/Opportunity/Quotation/Lost Quotation/Interested/
Converted/Do Not Contact).
"""

import frappe

from orion.migrate import load_jsonl, money, start_import

STATUS_MAP = {
	"PROSPECT": "Lead",
	"QUALIFIED": "Interested",
	"PROPOSAL": "Quotation",
	"WON": "Converted",
	"LOST": "Do Not Contact",
}


def run():
	start_import()
	created = 0
	total = 0
	for r in load_jsonl("leads"):
		total += 1
		if frappe.db.exists("Lead", {"orion_legacy_id": r["id"]}):
			continue

		doc = frappe.new_doc("Lead")
		doc.name = r["lead_code"]
		doc.lead_name = r["client_name"]
		doc.phone = r.get("client_phone")
		doc.status = STATUS_MAP.get(r["status"], "Lead")
		doc.orion_business_line = r.get("business_line")
		doc.orion_service_interest = r.get("service_interest")
		doc.orion_estimated_value = money(r.get("estimated_value"))
		doc.orion_short_name = r.get("short_name")
		doc.orion_lead_status = r["status"]
		doc.orion_legacy_id = r["id"]
		doc.flags.ignore_permissions = True
		doc.insert()
		created += 1
		if created % 100 == 0:
			frappe.db.commit()

	frappe.db.commit()
	print("leads: %s rows (created %s)" % (total, created))
