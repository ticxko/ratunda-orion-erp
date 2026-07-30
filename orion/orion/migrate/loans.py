"""Loans — bank_loans + loan_payments -> Orion Loan + Orion Loan Event
(plan §2 row 14).

bank_loans.jsonl columns (prisma, unmapped -> camelCase): id, loanNumber,
bankName, lenderType, lenderName, isRevolving, isCompanyLiability, docPrefix,
accountId, principal, interestRate, monthlyInstallment, tenorMonths,
startDate, maturityDate, outstandingBalance, status, businessLine, notes.
loan_payments.jsonl columns: id, bankLoanId, direction, docCode, date,
principalAmount, interestAmount, totalAmount, remainingBalance,
bankAccountId, journalEntryId, projectId, channel, reference, notes.

Notes:
- outstandingBalance is a cached projection in the source and is NOT stored;
  Orion Loan recomputes it from events. The run prints the recomputed figure
  per loan so it can be eyeballed against the source column (gate 5).
- loan_payments.bankAccountId (a CoA accounts.id despite the name) has no
  target field on Orion Loan Event and is not migrated.
- interestRate is a fraction in the source (Decimal(5,4), e.g. 0.1088);
  the Percent field wants 10.88, so it is multiplied by 100.
- Rows without a docCode get one generated the same way the app would
  (loan_codes port): loan docPrefix for drawdowns, POI-PB default for
  repayments, sequence continuing after the manually-coded rows.

Run:
    bench --site <site> execute orion.migrate.loans.run
"""

from datetime import datetime

import frappe

from orion.accounting.loan_codes import next_doc_code, recompute_outstanding
from orion.migrate import dec, load_jsonl, load_map, money, save_map, start_import, to_date


def run():
	start_import()
	accounts = load_map("accounts")
	projects = load_map("projects")
	je_map = load_map("journal_entries")

	idmap = import_loans(accounts)
	save_map("loans", idmap)
	import_events(idmap, je_map, projects)
	frappe.db.commit()
	print_outstanding(idmap)


def import_loans(accounts):
	idmap = {}
	created = 0
	total = 0
	for r in load_jsonl("bank_loans"):
		total += 1
		existing = frappe.db.get_value("Orion Loan", {"orion_legacy_id": r["id"]})
		if existing:
			idmap[r["id"]] = existing
			continue

		doc = frappe.new_doc("Orion Loan")
		doc.loan_number = loan_number(r)
		doc.bank_name = r.get("bankName")
		doc.lender_type = r.get("lenderType")
		doc.lender_name = r.get("lenderName")
		doc.is_revolving = 1 if r.get("isRevolving") else 0
		doc.is_company_liability = 1 if r.get("isCompanyLiability") else 0
		doc.doc_prefix = r.get("docPrefix")
		doc.account = accounts.get(r["accountId"]) if r.get("accountId") else None
		doc.principal = money(r.get("principal"))
		if r.get("interestRate") is not None:
			doc.interest_rate = float(dec(r["interestRate"]) * 100)
		doc.monthly_installment = money(r.get("monthlyInstallment"))
		doc.tenor_months = r.get("tenorMonths")
		doc.start_date = to_date(r.get("startDate"))
		doc.maturity_date = to_date(r.get("maturityDate"))
		doc.status = r.get("status")
		doc.business_line = r.get("businessLine")
		doc.notes = r.get("notes")
		doc.orion_legacy_id = r["id"]
		doc.flags.ignore_permissions = True
		doc.insert()
		idmap[r["id"]] = doc.name
		created += 1

	frappe.db.commit()
	print("loans: %s bank_loans (created %s)" % (total, created))
	return idmap


def loan_number(r):
	"""loanNumber is null for informal owner loans — fall back to the lender
	name (the facility's everyday identity), then the legacy id."""
	number = r.get("loanNumber") or r.get("lenderName") or r["id"]
	if not r.get("loanNumber"):
		print("loans: %s has no loanNumber, naming it %r" % (r["id"], number))
	return number


def import_events(loans, je_map, projects):
	prefixes = {r["id"]: r.get("docPrefix") for r in load_jsonl("bank_loans")}

	# date order keeps generated doc-code sequences continuous, like the app
	rows = sorted(load_jsonl("loan_payments"), key=lambda r: (r["date"], r["id"]))
	created = 0
	total = 0
	for r in rows:
		total += 1
		if frappe.db.exists("Orion Loan Event", {"orion_legacy_id": r["id"]}):
			continue

		loan = loans.get(r["bankLoanId"])
		if not loan:
			print("loan_events: SKIP %s — unmapped loan %s" % (r["id"], r["bankLoanId"]))
			continue

		je = None
		if r.get("journalEntryId"):
			je = je_map.get(r["journalEntryId"])
			if not je:
				print(
					"loan_events: WARNING %s — journal entry %s not migrated, leaving link blank"
					% (r["id"], r["journalEntryId"])
				)

		doc = frappe.new_doc("Orion Loan Event")
		doc.loan = loan
		doc.direction = r["direction"]
		doc.doc_code = r.get("docCode") or generate_doc_code(r, loan, prefixes)
		doc.date = to_date(r["date"])
		doc.principal_amount = money(r.get("principalAmount"))
		doc.interest_amount = money(r.get("interestAmount"))
		doc.total_amount = money(r.get("totalAmount"))
		doc.remaining_balance = money(r.get("remainingBalance"))
		doc.journal_entry = je
		doc.project = projects.get(r["projectId"]) if r.get("projectId") else None
		doc.channel = r.get("channel")
		doc.reference = r.get("reference")
		doc.notes = r.get("notes")
		doc.orion_legacy_id = r["id"]
		doc.flags.ignore_permissions = True
		doc.insert()
		created += 1

	frappe.db.commit()
	print("loan_events: %s rows (created %s)" % (total, created))


def generate_doc_code(r, loan, prefixes):
	"""Same rule as the app: loan docPrefix for drawdowns, POI-PB default
	for repayments; sequence counts existing events per loan+direction."""
	when = datetime.strptime(to_date(r["date"]), "%Y-%m-%d")
	prefix = prefixes.get(r["bankLoanId"]) if r["direction"] == "DRAWDOWN" else None
	code = next_doc_code(loan=loan, direction=r["direction"], when=when, prefix=prefix)
	print("loan_events: %s has no docCode, generated %s" % (r["id"], code))
	return code


def print_outstanding(loans):
	source = {r["id"]: dec(r.get("outstandingBalance")) for r in load_jsonl("bank_loans")}
	for legacy_id, name in sorted(loans.items()):
		outstanding = frappe.get_doc("Orion Loan", name).outstanding()
		flag = "" if outstanding == source.get(legacy_id) else "  (source: %s)" % source.get(legacy_id)
		print("loans: %s outstanding = %s%s" % (name, outstanding, flag))
