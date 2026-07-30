"""Field receipts (nota lapangan) — field_receipts + items + files ->
Orion Field Receipt (plan §2 row 17). Run after cash_advances.py.

field_receipts.jsonl columns (prisma @map -> snake_case): id, code,
project_id, disbursement_id, store_name, receipt_date, receipt_ref, notes,
status, total_amount.
field_receipt_items.jsonl columns: id, field_receipt_id, description,
category, unit, quantity, unit_price, amount, notes, sort_order.
field_receipt_files.jsonl columns: id, field_receipt_id, filename,
original_name, mime_type, file_size, local_path, gdrive_synced, uploaded_at.

File binaries are NOT migrated now — files_note keeps one
"local_path (original_name)" line per file so the scans can be attached
later. filename/mime_type/file_size/gdrive_synced are dropped with them.

Run:
    bench --site <site> execute orion.migrate.field_receipts.run
"""

import frappe

from orion.migrate import load_jsonl, load_map, money, start_import, to_date


def run():
	start_import()
	projects = load_map("projects")
	disbursements = load_map("disbursements")

	items_by_parent = {}
	for i in load_jsonl("field_receipt_items"):
		items_by_parent.setdefault(i["field_receipt_id"], []).append(i)

	files_by_parent = {}
	for f in load_jsonl("field_receipt_files"):
		files_by_parent.setdefault(f["field_receipt_id"], []).append(f)

	created = 0
	total = 0
	for r in load_jsonl("field_receipts"):
		total += 1
		if frappe.db.exists("Orion Field Receipt", {"orion_legacy_id": r["id"]}):
			continue

		cash_advance = None
		if r.get("disbursement_id"):
			cash_advance = disbursements.get(r["disbursement_id"])
			if not cash_advance:
				print(
					"field_receipts: WARNING %s — unmapped disbursement %s, leaving link blank"
					% (r["id"], r["disbursement_id"])
				)

		doc = frappe.new_doc("Orion Field Receipt")
		doc.code = r["code"]
		doc.project = projects.get(r["project_id"]) if r.get("project_id") else None
		doc.cash_advance = cash_advance
		doc.store_name = r.get("store_name")
		doc.receipt_date = to_date(r.get("receipt_date"))
		doc.receipt_ref = r.get("receipt_ref")
		doc.status = r["status"]
		doc.total_amount = money(r.get("total_amount"))
		doc.notes = r.get("notes")
		doc.files_note = files_note(files_by_parent.get(r["id"], []))
		doc.orion_legacy_id = r["id"]

		items = sorted(
			items_by_parent.get(r["id"], []), key=lambda i: (i.get("sort_order") or 0, i["id"])
		)
		for i in items:
			doc.append(
				"items",
				{
					"description": i.get("description"),
					"category": i.get("category"),
					"unit": i.get("unit"),
					"quantity": float(i["quantity"]) if i.get("quantity") is not None else None,
					"unit_price": money(i.get("unit_price")),
					"amount": money(i.get("amount")),
					"notes": i.get("notes"),
					"sort_order": i.get("sort_order"),
				},
			)

		doc.flags.ignore_permissions = True
		doc.insert()
		created += 1
		if created % 100 == 0:
			frappe.db.commit()

	frappe.db.commit()
	print("field_receipts: %s rows (created %s)" % (total, created))


def files_note(files):
	"""One 'local_path (original_name)' line per legacy file row."""
	lines = []
	for f in sorted(files, key=lambda f: (f.get("uploaded_at") or "", f["id"])):
		path = f.get("local_path") or f.get("localPath")
		original = f.get("original_name") or f.get("originalName")
		if path and original and original != path:
			lines.append("%s (%s)" % (path, original))
		elif path:
			lines.append(path)
		elif original:
			lines.append(original)
	return "\n".join(lines) or None
