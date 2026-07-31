"""One-off backfill of lead/client contact fields onto already-imported docs.

Phase A imported Lead/Customer with only names + status; this fills the
contact custom fields (added later) from the source JSONL, in place, by
orion_legacy_id. Idempotent — re-running just re-sets the same values.

    bench --site <site> execute orion.migrate.backfill_crm_contacts.run
"""

import frappe

from orion.migrate import load_jsonl, start_import


def run():
	start_import()
	leads = _backfill(
		"leads",
		"Lead",
		{
			"orion_client_salutation": "client_salutation",
			"orion_client_address": "client_address",
			"orion_source": "source",
			"orion_assigned_to": "assigned_to",
			"orion_notes": "notes",
			"phone": "client_phone",
		},
	)
	clients = _backfill(
		"clients",
		"Customer",
		{
			"orion_salutation": "salutation",
			"orion_phone": "phone",
			"orion_email": "email",
			"orion_address": "address",
			"orion_notes": "notes",
		},
	)
	frappe.db.commit()
	print("backfill_crm_contacts: %s leads, %s customers patched" % (leads, clients))


def _backfill(table, doctype, field_map):
	patched = 0
	for r in load_jsonl(table):
		name = frappe.db.get_value(doctype, {"orion_legacy_id": r["id"]})
		if not name:
			continue
		values = {dst: (r.get(src) or None) for dst, src in field_map.items()}
		frappe.db.set_value(doctype, name, values, update_modified=False)
		patched += 1
		if patched % 100 == 0:
			frappe.db.commit()
	return patched
