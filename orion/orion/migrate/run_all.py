"""Run the whole pipeline in dependency order.

    bench --site <site> execute orion.migrate.run_all.run
"""

import time

from orion.migrate import (
	banks,
	coa,
	company,
	customers,
	invoices,
	journal_entries,
	leads,
	programs,
	projects,
	service_types,
	suppliers,
	termins,
	users,
	validate,
)

STEPS = [
	("company", company),
	("coa", coa),
	("banks", banks),
	("users", users),
	("customers", customers),
	("suppliers", suppliers),
	("service_types", service_types),
	("leads", leads),
	("programs", programs),
	("projects", projects),
	("termins", termins),
	("journal_entries", journal_entries),
	("invoices", invoices),
	("validate", validate),
]


def run():
	grand_start = time.time()
	for label, module in STEPS:
		print("")
		print("=== %s ===" % label, flush=True)
		start = time.time()
		module.run()
		print("=== %s done in %.1fs ===" % (label, time.time() - start), flush=True)
	print("")
	print("run_all: finished in %.1fs" % (time.time() - grand_start))
