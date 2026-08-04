"""Attach field-receipt scan binaries to Orion Field Receipt records.

field_receipts.py deliberately skipped the scan binaries and left a `files_note`
pointer per file ("<local_path> (<original_name>)"). This step reads those
binaries from the legacy uploads tree — copied to <DATA_DIR>/notas (host:
/opt/orion/bellatrix/uploads/notas) — and creates a private Frappe File
attached to each receipt, so the feynman detail screen (and the ERPNext desk)
can serve the scans.

Idempotent: keyed on the file's content hash (matching Frappe's own
content_hash), so a scan already attached to the receipt is skipped even when
Frappe had to rename a same-named/different-content file on disk. Frappe also
deduplicates identical content across receipts (one monthly bill shared by many
notas stays a single stored file), which is intended. `files_note` is left
intact as the audit trail.

Prereq (host): copy the legacy tree next to the other migrate inputs, e.g.
    rsync -a /opt/orion/bellatrix/uploads/notas/ /opt/erpnext/migrate-data/notas/

Run (dry run first — reports what it would attach, writes nothing):
    bench --site <site> execute orion.migrate.attach_field_receipt_files.run --kwargs "{'dry_run': 1}"
    bench --site <site> execute orion.migrate.attach_field_receipt_files.run
"""

import hashlib
import os
import re

import frappe

from orion.migrate import DATA_DIR

# "<path> (<original name>)" — original is the trailing parenthetical.
_LINE = re.compile(r"^(?P<path>.*?)\s*\((?P<orig>.*)\)\s*$")


def _disk_path(rel: str) -> str | None:
	"""Map a files_note path onto the copied tree under DATA_DIR.

	Legacy paths look like 'uploads/notas/<proj>/<nota>/<file>'; the tree is
	copied to <DATA_DIR>/notas, so anchor on the 'notas/' segment (also tolerates
	an absolute legacy path)."""
	rel = (rel or "").strip().replace("\\", "/")
	idx = rel.find("notas/")
	if idx == -1:
		return None
	return os.path.join(DATA_DIR, rel[idx:])


def _parse(files_note: str):
	"""Yield (disk_path, original_name) per non-empty line of files_note."""
	for raw in (files_note or "").splitlines():
		line = raw.strip()
		if not line:
			continue
		m = _LINE.match(line)
		if m:
			path, orig = m.group("path").strip(), m.group("orig").strip()
		else:
			path, orig = line, os.path.basename(line)
		yield _disk_path(path), (orig or (os.path.basename(path) if path else None))


def run(dry_run=0):
	dry_run = int(dry_run)
	rows = frappe.get_all(
		"Orion Field Receipt",
		filters={"files_note": ("is", "set")},
		fields=["name", "files_note"],
	)
	created = skipped = missing = 0
	for r in rows:
		existing_hashes = set(
			frappe.get_all(
				"File",
				filters={"attached_to_doctype": "Orion Field Receipt", "attached_to_name": r.name},
				pluck="content_hash",
			)
		)
		for disk, original in _parse(r.files_note):
			if not original:
				skipped += 1
				continue
			if not disk or not os.path.exists(disk):
				missing += 1
				print("attach_field_receipt_files: MISSING %s -> %s" % (r.name, disk))
				continue
			with open(disk, "rb") as fh:
				content = fh.read()
			chash = hashlib.md5(content, usedforsecurity=False).hexdigest()
			if chash in existing_hashes:
				skipped += 1
				continue
			if dry_run:
				print("attach_field_receipt_files: WOULD ATTACH %s <- %s" % (r.name, original))
				existing_hashes.add(chash)
				created += 1
				continue
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": original,
					"attached_to_doctype": "Orion Field Receipt",
					"attached_to_name": r.name,
					"is_private": 1,
					"content": content,
				}
			).insert(ignore_permissions=True)
			existing_hashes.add(chash)
			created += 1
			if created % 50 == 0:
				frappe.db.commit()

	if not dry_run:
		frappe.db.commit()
	print(
		"attach_field_receipt_files: %s%s attached, %s skipped, %s missing (over %s receipts)"
		% ("[DRY RUN] " if dry_run else "", created, skipped, missing, len(rows))
	)
