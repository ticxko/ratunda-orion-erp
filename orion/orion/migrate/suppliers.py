"""Step 4b — suppliers.

vendors.jsonl columns: id, code, name, type, phone, address, bankName,
bankAccount, accountName, notes, isActive.
"""

import frappe

from orion.migrate import load_jsonl, save_map, start_import

TYPE_GROUP = {
	"MATERIAL_SUPPLIER": "Material Supplier",
	"SUBCONTRACTOR": "Subcontractor",
	"EQUIPMENT_RENTAL": "Equipment Rental",
	"OTHER": "Other",
}


def run():
	start_import()
	for group in TYPE_GROUP.values():
		ensure_supplier_group(group)

	idmap = {}
	created = 0
	for r in load_jsonl("vendors"):
		existing = frappe.db.get_value("Supplier", {"orion_legacy_id": r["id"]})
		if existing:
			idmap[r["id"]] = existing
			continue

		doc = frappe.new_doc("Supplier")
		doc.supplier_name = r["name"]
		doc.supplier_group = TYPE_GROUP.get(r["type"], "Other")
		doc.orion_vendor_code = r["code"]
		doc.disabled = 0 if r.get("isActive", True) else 1
		doc.orion_legacy_id = r["id"]
		doc.flags.ignore_permissions = True
		doc.insert()
		idmap[r["id"]] = doc.name
		created += 1
		if created % 100 == 0:
			frappe.db.commit()

	save_map("suppliers", idmap)
	frappe.db.commit()
	print("suppliers: %s suppliers (created %s)" % (len(idmap), created))


def ensure_supplier_group(group):
	if frappe.db.exists("Supplier Group", group):
		return
	root = frappe.db.get_value("Supplier Group", {"is_group": 1, "parent_supplier_group": ("is", "not set")})
	doc = frappe.new_doc("Supplier Group")
	doc.supplier_group_name = group
	doc.parent_supplier_group = root
	doc.is_group = 0
	doc.flags.ignore_permissions = True
	doc.insert()
