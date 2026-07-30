"""Orion -> ERPNext data migration package.

Every module exposes run() so it can be executed with:
    bench --site <site> execute orion.migrate.<module>.run

Source data: one JSONL file per Postgres table (row_to_json, raw column
names) mounted read-only at DATA_DIR.
"""

import json
import os
from decimal import Decimal

import frappe

DATA_DIR = os.environ.get("ORION_MIGRATE_DATA", "/home/frappe/migrate-data")


def load_jsonl(table):
	"""Yield one dict per line of <DATA_DIR>/<table>.jsonl. Floats become Decimal."""
	path = os.path.join(DATA_DIR, table + ".jsonl")
	with open(path) as f:
		for line in f:
			line = line.strip()
			if line:
				yield json.loads(line, parse_float=Decimal)


def maps_dir():
	path = frappe.get_site_path("private", "migrate-maps")
	os.makedirs(path, exist_ok=True)
	return path


def save_map(name, mapping):
	with open(os.path.join(maps_dir(), name + ".json"), "w") as f:
		json.dump(mapping, f, indent=1, sort_keys=True, default=str)


def load_map(name):
	path = os.path.join(maps_dir(), name + ".json")
	if not os.path.exists(path):
		return {}
	with open(path) as f:
		return json.load(f)


def to_date(value):
	"""ISO timestamp/date string -> YYYY-MM-DD (or None)."""
	return value[:10] if value else None


def dec(value):
	"""Anything -> Decimal, exactly. None -> 0."""
	if value is None:
		return Decimal("0")
	if isinstance(value, Decimal):
		return value
	return Decimal(str(value))


def money(value):
	"""Decimal-exact 2dp value as float for Currency fields (None stays None)."""
	if value is None:
		return None
	return float(dec(value).quantize(Decimal("0.01")))


def get_settings():
	return frappe.get_doc("Orion Settings")


def start_import():
	frappe.flags.in_import = True
