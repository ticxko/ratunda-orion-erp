"""Step 6 — bank statement job archive (116 rows, GL-neutral).

bank_statement_jobs.jsonl columns (prisma, mapped -> snake_case where @map'd):
id, status, fileName, result (JSON), error, source, reviewed, reviewed_at,
created_at, updated_at.

Jobs import as history: result stored as-is (JSON string), reviewed flags
preserved. NO Bank Transactions are created for these rows — their Journal
Entries already exist from the GL history import (step 7a) and go-forward
dual-writes only apply to new approvals.

Smoke run:
	bench --site <site> execute orion.migrate.bank_statement_jobs.run --kwargs "{'limit': 5}"
"""

import json
from decimal import Decimal

import frappe

from orion.migrate import load_jsonl, load_map, save_map, start_import

DOCTYPE = "Orion Bank Statement Job"


def _json_default(o):
	if isinstance(o, Decimal):
		return float(o)
	raise TypeError("Not JSON serializable: %r" % (o,))


def run(limit=None):
	start_import()
	job_map = load_map("bank_statement_jobs")
	created = 0
	seen = 0

	for r in load_jsonl("bank_statement_jobs"):
		if limit and seen >= int(limit):
			break
		seen += 1

		existing = frappe.db.get_value(DOCTYPE, {"orion_legacy_id": r["id"]})
		if existing:
			job_map[r["id"]] = existing
			continue

		result = r.get("result")
		result_str = (
			json.dumps(result, ensure_ascii=False, default=_json_default)
			if result is not None
			else None
		)
		info = (result or {}).get("info") or {}

		doc = frappe.new_doc(DOCTYPE)
		doc.status = r.get("status") or "pending"
		doc.file_name = r.get("fileName")
		doc.source = r.get("source") or "manual"
		doc.result = result_str
		doc.error = r.get("error")
		doc.reviewed = 1 if r.get("reviewed") else 0
		doc.reviewed_at = _ts(r.get("reviewed_at"))
		doc.bank_account_no = info.get("accountNo")
		doc.orion_legacy_id = r["id"]
		doc.flags.ignore_permissions = True
		doc.insert()

		# Preserve source timestamps for inbox ordering / audit.
		frappe.db.set_value(
			DOCTYPE,
			doc.name,
			{
				"creation": _ts(r.get("created_at")) or doc.creation,
				"modified": _ts(r.get("updated_at")) or doc.modified,
			},
			update_modified=False,
		)

		job_map[r["id"]] = doc.name
		created += 1
		if created % 50 == 0:
			frappe.db.commit()
			print("bank_statement_jobs: %s inserted" % created, flush=True)

	save_map("bank_statement_jobs", job_map)
	frappe.db.commit()
	print(
		"bank_statement_jobs: %s created, %s already there (of %s seen)"
		% (created, len(job_map) - created, seen)
	)


def _ts(value):
	"""ISO timestamp string -> 'YYYY-MM-DD HH:MM:SS' (or None)."""
	if not value:
		return None
	return str(value).replace("T", " ")[:19]
