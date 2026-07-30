"""Step 6a — purchase orders (plan §2 row 11). Run after suppliers + projects.

purchase_orders.jsonl columns (raw Postgres names per prisma @map): id, number,
projectId, vendorId, status, po_type, marketplace, online_order_id, orderDate,
expectedDate, received_date, cancelled_date, cancel_reason, notes, createdAt,
updatedAt.
po_items.jsonl columns: id, purchaseOrderId, description, unit, quantity,
unitPrice, totalPrice, notes, createdAt.

Mapping:
  - PO number becomes doc.name (preset, start_import()'s in_import flag).
  - Every row books against one of four generic non-stock service Items
    (ORION-MATERIAL / ORION-UPAH / ORION-OPERASIONAL / ORION-TRANSPORT, all
    ensured here under a leaf "Orion Services" Item Group). po_items carry no
    category, so PO rows all use ORION-MATERIAL; the other three exist for the
    receipt-side consumers of the same Item set. Source description/unit/notes
    survive on the row (description + orion_unit/orion_notes custom fields);
    row uom stays Nos so no UOM conversions are ever needed.
  - supplier via the suppliers idmap. The live vendors table is EMPTY and a PO
    row carries only vendorId (no vendor fields to build a Supplier from), so
    a PO referencing an unmapped vendor is REJECTED with a print.
  - status: RECEIVED -> submitted + status Closed (historical, no receipt/AP
    linkage — matches Orion); everything else stays draft. The source status
    and its dates survive verbatim in orion_status / orion_received_date /
    orion_cancelled_date / orion_cancel_reason for the compat layer.
  - schedule_date = expectedDate or orderDate (PO requires it), clamped to
    orderDate at minimum (ERPNext forbids schedule before transaction date).
  - company from Orion Settings; rows carry the project and its brand cost
    center (Project.cost_center was set per businessLine by projects.py).

Run:
    bench --site <site> execute orion.migrate.purchase_orders.run
"""

import frappe

from orion.migrate import get_settings, load_jsonl, load_map, money, save_map, start_import, to_date

ITEM_GROUP = "Orion Services"
CATEGORIES = ("MATERIAL", "UPAH", "OPERASIONAL", "TRANSPORT")
PO_ROW_ITEM = "ORION-MATERIAL"


def run():
	start_import()
	settings = get_settings()
	ensure_items(settings.company)

	suppliers = load_map("suppliers")
	projects = load_map("projects")

	items_by_po = {}
	for i in load_jsonl("po_items"):
		items_by_po.setdefault(i["purchaseOrderId"], []).append(i)
	for rows in items_by_po.values():
		rows.sort(key=lambda i: (i.get("createdAt") or "", i["id"]))

	idmap = load_map("purchase_orders")
	created = 0
	submitted = 0
	rejected = 0
	total = 0
	for r in load_jsonl("purchase_orders"):
		total += 1
		existing = frappe.db.get_value("Purchase Order", {"orion_legacy_id": r["id"]})
		if existing:
			idmap[r["id"]] = existing
			continue

		supplier = suppliers.get(r["vendorId"]) or frappe.db.get_value(
			"Supplier", {"orion_legacy_id": r["vendorId"]}
		)
		if not supplier:
			print(
				"purchase_orders: REJECT %s %s — vendor %s has no Supplier "
				"(vendors table was empty; PO rows carry no vendor fields)"
				% (r["id"], r.get("number"), r["vendorId"])
			)
			rejected += 1
			continue

		rows = items_by_po.get(r["id"], [])
		if not rows:
			print("purchase_orders: REJECT %s %s — no po_items" % (r["id"], r.get("number")))
			rejected += 1
			continue

		project = projects.get(r["projectId"]) if r.get("projectId") else None
		cost_center = (
			frappe.db.get_value("Project", project, "cost_center") if project else None
		) or settings.shared_cost_center

		order_date = to_date(r.get("orderDate")) or to_date(r.get("createdAt"))
		schedule = to_date(r.get("expectedDate")) or order_date
		if order_date and schedule < order_date:
			schedule = order_date

		doc = frappe.new_doc("Purchase Order")
		doc.name = r["number"]
		doc.company = settings.company
		doc.supplier = supplier
		doc.currency = "IDR"
		doc.conversion_rate = 1
		doc.transaction_date = order_date
		doc.schedule_date = schedule
		doc.project = project
		doc.cost_center = cost_center
		doc.orion_po_type = r.get("po_type") or "PO"
		doc.orion_marketplace = r.get("marketplace")
		doc.orion_online_order_id = r.get("online_order_id")
		doc.orion_status = r.get("status")
		doc.orion_received_date = to_date(r.get("received_date"))
		doc.orion_cancelled_date = to_date(r.get("cancelled_date"))
		doc.orion_cancel_reason = r.get("cancel_reason")
		doc.orion_notes = r.get("notes")
		doc.orion_legacy_id = r["id"]

		for i in rows:
			label = (i.get("description") or "").strip() or "PO item"
			doc.append(
				"items",
				{
					"item_code": PO_ROW_ITEM,
					"item_name": label[:140],
					"description": label,
					"qty": float(i["quantity"]) if i.get("quantity") is not None else 1,
					"rate": money(i.get("unitPrice")) or 0,
					"uom": "Nos",
					"stock_uom": "Nos",
					"conversion_factor": 1,
					"schedule_date": schedule,
					"cost_center": cost_center,
					"project": project,
					"orion_unit": i.get("unit"),
					"orion_notes": i.get("notes"),
				},
			)

		doc.flags.ignore_permissions = True
		doc.insert()
		if r.get("status") == "RECEIVED":
			doc.submit()
			# Closed = historical, nothing left to receive/bill; set directly
			# (update_status would demand receipts that were never migrated)
			frappe.db.set_value(
				"Purchase Order", doc.name, "status", "Closed", update_modified=False
			)
			submitted += 1
		idmap[r["id"]] = doc.name
		created += 1
		if created % 100 == 0:
			frappe.db.commit()

	save_map("purchase_orders", idmap)
	frappe.db.commit()
	print(
		"purchase_orders: %s rows (created %s, submitted+Closed %s, rejected %s)"
		% (total, created, submitted, rejected)
	)


def ensure_items(company):
	"""The four generic non-stock service Items + their leaf Item Group."""
	ensure_item_group()
	for cat in CATEGORIES:
		code = "ORION-%s" % cat
		if frappe.db.exists("Item", code):
			continue
		doc = frappe.new_doc("Item")
		doc.item_code = code
		doc.item_name = "Orion %s" % cat.capitalize()
		doc.item_group = ITEM_GROUP
		doc.stock_uom = "Nos"
		doc.is_stock_item = 0
		doc.is_sales_item = 0
		doc.is_purchase_item = 1
		doc.include_item_in_manufacturing = 0
		doc.description = "Generic Orion %s line (non-stock service item)" % cat.capitalize()
		doc.flags.ignore_permissions = True
		doc.insert()
		print("purchase_orders: created Item %s" % code)


def ensure_item_group():
	if frappe.db.exists("Item Group", ITEM_GROUP):
		return
	root = frappe.db.get_value(
		"Item Group", {"is_group": 1, "parent_item_group": ("is", "not set")}
	)
	doc = frappe.new_doc("Item Group")
	doc.item_group_name = ITEM_GROUP
	doc.parent_item_group = root
	doc.is_group = 0
	doc.flags.ignore_permissions = True
	doc.insert()
	print("purchase_orders: created Item Group %s" % ITEM_GROUP)
