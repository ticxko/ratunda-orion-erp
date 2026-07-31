"""Step 4a — customers.

clients.jsonl columns: id, name, salutation, phone, email, address, notes,
is_active.
"""

import frappe

from orion.migrate import load_jsonl, save_map, start_import


def run():
	start_import()
	group = ensure_customer_group()
	territory = ensure_territory()

	idmap = {}
	created = 0
	for r in load_jsonl("clients"):
		existing = frappe.db.get_value("Customer", {"orion_legacy_id": r["id"]})
		if existing:
			idmap[r["id"]] = existing
			continue

		# distinct source clients can share a name; Customer is named by it
		customer_name = r["name"]
		n = 1
		while frappe.db.exists("Customer", customer_name):
			n += 1
			customer_name = "%s (%s)" % (r["name"], n)

		doc = frappe.new_doc("Customer")
		doc.customer_name = customer_name
		doc.customer_type = "Individual"
		doc.customer_group = group
		doc.territory = territory
		doc.disabled = 0 if r.get("is_active", True) else 1
		doc.orion_salutation = r.get("salutation")
		doc.orion_phone = r.get("phone")
		doc.orion_email = r.get("email")
		doc.orion_address = r.get("address")
		doc.orion_notes = r.get("notes")
		doc.orion_legacy_id = r["id"]
		doc.flags.ignore_permissions = True
		doc.insert()
		idmap[r["id"]] = doc.name
		created += 1
		if created % 100 == 0:
			frappe.db.commit()

	save_map("customers", idmap)
	frappe.db.commit()
	print("customers: %s customers (created %s)" % (len(idmap), created))


def ensure_customer_group():
	# Customer requires a NON-group Customer Group — never the root
	if frappe.db.exists("Customer Group", "Orion"):
		return "Orion"
	root = frappe.db.get_value("Customer Group", {"is_group": 1, "parent_customer_group": ("is", "not set")})
	doc = frappe.new_doc("Customer Group")
	doc.customer_group_name = "Orion"
	doc.parent_customer_group = root
	doc.is_group = 0
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def ensure_territory():
	# leaf territory for the same reason
	if frappe.db.exists("Territory", "Indonesia"):
		return "Indonesia"
	root = frappe.db.get_value("Territory", {"is_group": 1, "parent_territory": ("is", "not set")})
	doc = frappe.new_doc("Territory")
	doc.territory_name = "Indonesia"
	doc.parent_territory = root
	doc.is_group = 0
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name
