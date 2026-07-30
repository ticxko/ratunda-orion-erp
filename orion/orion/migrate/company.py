"""Step 0 — company bootstrap.

Runs the ERPNext setup wizard headlessly if the site has no Company yet,
then ensures FY 2025, the three brand cost centers, Orion Settings and the
Orion roles.

Verified against v16 sources: frappe.desk.page.setup_wizard.setup_wizard.
setup_complete(args) drives erpnext's setup_wizard_stages hook, whose
install_company() reads args.company_name / company_abbr / currency /
country / chart_of_accounts / fy_start_date / fy_end_date / domain.
language must be "English" (full name) — a non-"English" value triggers
set_default_language(args.lang). No "bank_account" key -> no demo bank
account is created.
"""

import datetime

import frappe

from orion.migrate import start_import

COMPANY_NAME = "PT Pencipta Organik Imaji"
COMPANY_ABBR = "POI"
COST_CENTERS = ["Ratunda Renovasi", "Poiesis Studio", "Shared"]
ROLES = ["Orion Admin", "Orion Project Admin"]


def run():
	start_import()
	bootstrap_company()
	ensure_fiscal_year("2025", "2025-01-01", "2025-12-31")
	ccs = ensure_cost_centers()
	write_settings(ccs)
	ensure_roles()
	frappe.db.commit()
	print("company: bootstrap done —", COMPANY_NAME)


def bootstrap_company():
	if frappe.db.count("Company"):
		return

	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	year = datetime.date.today().year
	args = {
		"language": "English",
		"country": "Indonesia",
		"currency": "IDR",
		"timezone": "Asia/Jakarta",
		"company_name": COMPANY_NAME,
		"company_abbr": COMPANY_ABBR,
		"chart_of_accounts": "Standard",
		"fy_start_date": "%s-01-01" % year,
		"fy_end_date": "%s-12-31" % year,
		"domain": "Services",
		"setup_demo": 0,
		"enable_telemetry": 0,
	}
	setup_complete(args)

	if not frappe.db.exists("Company", COMPANY_NAME):
		frappe.throw("Setup wizard did not create company %s" % COMPANY_NAME)
	print("company: setup wizard completed")


def ensure_fiscal_year(year, start, end):
	if frappe.db.exists("Fiscal Year", year):
		return
	fy = frappe.new_doc("Fiscal Year")
	fy.year = year
	fy.year_start_date = start
	fy.year_end_date = end
	fy.flags.ignore_permissions = True
	fy.insert()
	print("company: created Fiscal Year", year)


def ensure_cost_centers():
	root = frappe.db.get_value(
		"Cost Center",
		{"company": COMPANY_NAME, "is_group": 1, "parent_cost_center": ("is", "not set")},
	)
	if not root:
		frappe.throw("Root cost center for %s not found" % COMPANY_NAME)

	out = {}
	for label in COST_CENTERS:
		name = frappe.db.get_value(
			"Cost Center", {"company": COMPANY_NAME, "cost_center_name": label}
		)
		if not name:
			cc = frappe.new_doc("Cost Center")
			cc.cost_center_name = label
			cc.parent_cost_center = root
			cc.company = COMPANY_NAME
			cc.is_group = 0
			cc.flags.ignore_permissions = True
			cc.insert()
			name = cc.name
			print("company: created Cost Center", name)
		out[label] = name
	return out


def write_settings(ccs):
	settings = frappe.get_doc("Orion Settings")
	settings.company = COMPANY_NAME
	settings.ratunda_cost_center = ccs["Ratunda Renovasi"]
	settings.poiesis_cost_center = ccs["Poiesis Studio"]
	settings.shared_cost_center = ccs["Shared"]
	settings.flags.ignore_permissions = True
	settings.save()


def ensure_roles():
	for role in ROLES:
		if frappe.db.exists("Role", role):
			continue
		doc = frappe.new_doc("Role")
		doc.role_name = role
		doc.desk_access = 1
		doc.flags.ignore_permissions = True
		doc.insert()
		print("company: created Role", role)
