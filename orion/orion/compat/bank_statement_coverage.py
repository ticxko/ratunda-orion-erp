"""Bank Statement Record compat handler — monthly upload-completion status per
company bank account.

Routes served through the JSON gateway (orion.compat.handle):

  GET  /api/accounting/bank-statement/coverage
       -> {"months": [{year, month, label}, ...current + last 5...],
           "accounts": [{accountNo, bankName, owner, glCode, hasInbox,
                         cells: {"<year>-<month>": {status, recordedCount,
                                 recordedDays, expectedCount, periodLabel,
                                 fullMonthJob}}}]}
       Cells are read from the stored "Orion Bank Statement Coverage" doctype;
       any cell missing for the requested window is computed + stored on the fly
       (the ledger is not rescanned once a cell exists).

  POST /api/accounting/bank-statement/coverage/recompute
       payload {} -> recompute every company account × window cell
       payload {"accountNo": "..."} -> recompute that account's window cells

This prefix is longer than bank_statement.py's "/api/accounting/bank-statement",
so the gateway's longest-prefix-wins routing sends /coverage here.
"""

import frappe

from orion.accounting import statement_coverage as cov
from orion.compat.handle import route, split_path

PREFIX = "/api/accounting/bank-statement/coverage"


@route(PREFIX)
def coverage(path: str, verb: str, payload: dict):
	bare, _query = split_path(path)
	rest = bare[len(PREFIX):].strip("/")
	parts = [p for p in rest.split("/") if p]

	if verb == "GET" and not parts:
		return _get_coverage()
	if verb == "POST" and parts == ["recompute"]:
		return _recompute((payload or {}).get("accountNo"))
	frappe.throw("No compat handler for %s %s" % (verb, bare), exc=frappe.DoesNotExistError)


def _cell_out(cell: dict) -> dict:
	return {
		"status": cell["status"],
		"recordedCount": cell["recordedCount"],
		"recordedDays": cell["recordedDays"],
		"expectedCount": cell["expectedCount"],
		"periodLabel": cell["periodLabel"],
		"fullMonthJob": cell["fullMonthJob"],
	}


def _get_coverage() -> dict:
	months = cov.recent_months()
	accounts = cov.company_accounts()
	out_accounts = []
	for acc in accounts:
		cells = {}
		for m in months:
			cell = cov.get_or_compute(acc["accountNo"], m["year"], m["month"])
			cells["%d-%d" % (m["year"], m["month"])] = _cell_out(cell)
		out_accounts.append({**acc, "cells": cells})
	return {"months": months, "accounts": out_accounts}


def _recompute(account_no: str | None) -> dict:
	months = cov.recent_months()
	accounts = cov.company_accounts()
	if account_no:
		accounts = [a for a in accounts if a["accountNo"] == account_no]
		if not accounts:
			frappe.throw("Unknown company account %s" % account_no, exc=frappe.DoesNotExistError)
	for acc in accounts:
		for m in months:
			cov.recompute(acc["accountNo"], m["year"], m["month"])
	frappe.db.commit()
	return _get_coverage()
