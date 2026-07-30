"""Cash advances — disbursements + disbursement_items -> Orion Cash Advance
(plan §2 row 15).

disbursements.jsonl columns (prisma @map -> snake_case): id, code,
project_id, business_line, ca_type, title, requested_by, prepared_by,
status, target_account_id, transfer_date, keterangan_transfer, total_amount,
journal_entry_id, source_file, physical_id, notes.
disbursement_items.jsonl columns: id, disbursement_id, tahap_pekerjaan,
item, spek_ukuran, volume, satuan, durasi_value, durasi_unit, category,
unit_price, amount, gl_account_code, sort_order.

Saves the "disbursements" idmap for field_receipts.py. Status is carried
verbatim (workflow states for new docs come later).

Run:
    bench --site <site> execute orion.migrate.cash_advances.run
"""

import frappe

from orion.migrate import load_jsonl, load_map, money, save_map, start_import, to_date


def run():
	start_import()
	projects = load_map("projects")
	bank_accounts = load_map("bank_accounts")
	je_map = load_map("journal_entries")

	items_by_parent = {}
	for i in load_jsonl("disbursement_items"):
		items_by_parent.setdefault(i["disbursement_id"], []).append(i)

	idmap = {}
	created = 0
	total = 0
	for r in load_jsonl("disbursements"):
		total += 1
		existing = frappe.db.get_value("Orion Cash Advance", {"orion_legacy_id": r["id"]})
		if existing:
			idmap[r["id"]] = existing
			continue

		je = None
		if r.get("journal_entry_id"):
			je = je_map.get(r["journal_entry_id"])
			if not je:
				print(
					"cash_advances: WARNING %s — journal entry %s not migrated, leaving link blank"
					% (r["id"], r["journal_entry_id"])
				)

		doc = frappe.new_doc("Orion Cash Advance")
		doc.code = r["code"]
		doc.project = projects.get(r["project_id"]) if r.get("project_id") else None
		doc.business_line = r.get("business_line")
		doc.ca_type = r.get("ca_type")
		doc.title = r.get("title")
		doc.requested_by = r.get("requested_by")
		doc.prepared_by = r.get("prepared_by")
		doc.status = r["status"]
		doc.target_bank_account = (
			bank_accounts.get(r["target_account_id"]) if r.get("target_account_id") else None
		)
		doc.transfer_date = to_date(r.get("transfer_date"))
		doc.keterangan_transfer = r.get("keterangan_transfer")
		doc.total_amount = money(r.get("total_amount"))
		doc.journal_entry = je
		doc.source_file = r.get("source_file")
		doc.physical_id = r.get("physical_id")
		doc.notes = r.get("notes")
		doc.orion_legacy_id = r["id"]

		items = sorted(
			items_by_parent.get(r["id"], []), key=lambda i: (i.get("sort_order") or 0, i["id"])
		)
		for i in items:
			doc.append(
				"items",
				{
					"tahap_pekerjaan": i.get("tahap_pekerjaan"),
					"item": i.get("item"),
					"spek_ukuran": i.get("spek_ukuran"),
					"volume": float(i["volume"]) if i.get("volume") is not None else None,
					"satuan": i.get("satuan"),
					"durasi_value": str(i["durasi_value"]) if i.get("durasi_value") is not None else None,
					"durasi_unit": i.get("durasi_unit"),
					"category": i.get("category"),
					"unit_price": money(i.get("unit_price")),
					"amount": money(i.get("amount")),
					"gl_account_code": i.get("gl_account_code"),
					"sort_order": i.get("sort_order"),
				},
			)

		doc.flags.ignore_permissions = True
		doc.insert()
		idmap[r["id"]] = doc.name
		created += 1

	save_map("disbursements", idmap)
	frappe.db.commit()
	print("cash_advances: %s disbursements (created %s)" % (total, created))
