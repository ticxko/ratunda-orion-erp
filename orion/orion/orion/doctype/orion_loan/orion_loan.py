# Copyright (c) 2026, PT Pencipta Organik Imaji and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from orion.accounting.loan_codes import recompute_outstanding


class OrionLoan(Document):
	def outstanding(self):
		"""Outstanding balance, always recomputed from the loan's events.

		Sum of DRAWDOWN principal less sum of REPAYMENT principal, floored
		at zero — never stored, never incremented in place.
		"""
		rows = frappe.db.sql(
			"""
			select direction, coalesce(sum(principal_amount), 0)
			from `tabOrion Loan Event`
			where loan = %s
			group by direction
			""",
			self.name,
		)
		return recompute_outstanding(rows)
