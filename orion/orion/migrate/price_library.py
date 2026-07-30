"""Step 6b — price library: NOTHING TO IMPORT (decision record + parity check).

Investigated for this step (prisma/schema.prisma has no PriceLibrary model,
no price-library @@map): the legacy "price library" is not a table. The Node
endpoint GET /api/supply-chain/price-library
(bellatrix src/routes/supply-chain/price-library.ts) is a live SQL aggregation
over field_receipt_items JOIN field_receipts WHERE status = 'VERIFIED',
grouped by (description, unit, category) with last/avg/min/max unit_price,
count, last_seen and the last 3 project refs per (description, unit).

Field receipts (and their items) are already migrated 1:1 into Orion Field
Receipt by field_receipts.py, so the library re-derives losslessly on the
ERPNext side: compat/supply_chain.py serves /api/supply-chain/price-library by
aggregating Orion Field Receipt Item rows live — no doctype, no snapshot, no
staleness. Hence this module imports nothing and there is no
"price_library.jsonl" to extract.

What run() does instead: recompute the expected aggregate from the source
JSONL and print it, as a parity gate for the compat endpoint (row count must
match `len(items)` returned by GET /api/supply-chain/price-library once field
receipts are imported). Pure file reading — safe to run any time.

Run:
    bench --site <site> execute orion.migrate.price_library.run
"""

from orion.migrate import load_jsonl


def run():
	verified = {
		r["id"] for r in load_jsonl("field_receipts") if r.get("status") == "VERIFIED"
	}
	groups = set()
	items = 0
	for i in load_jsonl("field_receipt_items"):
		if i["field_receipt_id"] not in verified:
			continue
		items += 1
		groups.add((i.get("description"), i.get("unit"), i.get("category")))

	print(
		"price_library: nothing to import — legacy price library is a live "
		"aggregation over field_receipt_items (see module docstring)."
	)
	print(
		"price_library: source parity — %s VERIFIED receipts, %s priced item "
		"rows, %s distinct (description, unit, category) library rows expected "
		"from compat GET /api/supply-chain/price-library" % (len(verified), items, len(groups))
	)
