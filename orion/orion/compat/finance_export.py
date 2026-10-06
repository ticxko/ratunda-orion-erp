"""Excel export for the accounting module.

One whitelisted GET endpoint builds a workbook from any combination of nine
sheets, bounded by a month-to-month period and optionally broken into per-month
columns. Binary cannot ride the JSON gateway (orion.compat.handle), so feynman
hits this method directly with the session cookie, exactly like
expense_report.export_xlsx.

The cardinal rule here is that **exported numbers must tie out to the on-screen
reports**. That is achieved structurally, not by testing: this module computes
no accounting arithmetic of its own. It calls the same builders the SPA's
endpoints call (_trial_balance_from, _profit_loss_from, _balance_sheet_from,
_gl_blocks, _cash_flow in orion.compat.accounting) and merely feeds them
per-month aggregates instead of one period aggregate. If someone later changes
_orion_type, _bl_ok or the EQUITY+DEBIT parity flip, screen and workbook move
together.

Layout and styling live in finance_export_xlsx; the pure algorithms that must
be right (monthly folding, the CoA tree rollup, cash-flow attribution) live in
orion.accounting.report_math, which is unit-tested.
"""

import os
from io import BytesIO

import frappe
from frappe.utils import cint, now_datetime

from orion.accounting.report_math import account_tree, aging_bucket, flatten_tree
from orion.compat.accounting import (
	_account_rows,
	_aggregate,
	_aggregate_monthly,
	_balance_sheet_from,
	_bl_ok,
	_bs_accounts,
	_cash_flow,
	_company,
	_gl_blocks,
	_normal_balance,
	_orion_type,
	_parse_date,
	_pl_accounts,
	_profit_loss_from,
	_resolve_account,
	_tb_accounts,
	_trial_balance_from,
)
from orion.compat.expense_report_xlsx import _date_id
from orion.compat.finance_export_xlsx import (
	MONEY_FMT,
	_month_end_label,
	_month_label,
	build_workbook,
	theme_for,
)

SHEET_KEYS = (
	"coa", "ledger", "trial-balance", "profit-loss",
	"balance-sheet", "cash-flow", "journal", "ar",
)
# A per-month breakdown only means something for a report that is already a
# period total or a point-in-time balance. On a transaction listing (ledger,
# journal) or a master list (CoA) it is meaningless, so it is ignored there.
MONTHLY_CAPABLE = ("trial-balance", "profit-loss", "balance-sheet", "cash-flow")

BL_LABEL = {
	"ALL": "SEMUA",
	"RATUNDA_RENOVASI": "Ratunda Renovasi",
	"POIESIS_STUDIO": "Poiesis Studio",
}

RUPIAH_NOTE = "Nilai dalam Rupiah"


# ── endpoint ────────────────────────────────────────────────────────────────


@frappe.whitelist(methods=["GET"])
def export_xlsx(
	fromDate=None,
	toDate=None,
	asOf=None,
	sheets=None,
	businessLine="ALL",
	monthly=0,
	accountId=None,
):
	"""Build and return the workbook as a binary download.

	Params are camelCase, not the `from`/`to` the gateway routes use: `from` is
	a Python keyword and frappe.whitelist splats form_dict straight into kwargs,
	so `def export_xlsx(from=...)` would not even parse.

	`sheets` is a comma-separated key list, so a per-screen Export button passes
	one key and the Ekspor Excel page passes many — one endpoint rather than
	eight.
	"""
	if frappe.session.user in ("Guest", "", None):
		raise frappe.AuthenticationError

	keys = [k.strip() for k in (sheets or "").split(",") if k.strip()]
	unknown = [k for k in keys if k not in SHEET_KEYS]
	if unknown:
		frappe.throw("Unknown sheet(s): %s" % ", ".join(unknown))
	if not keys:
		keys = list(SHEET_KEYS)

	wb, filename = _build(
		fromDate, toDate, asOf, keys, businessLine or "ALL",
		cint(monthly), accountId,
	)
	buf = BytesIO()
	wb.save(buf)
	frappe.response["filename"] = filename
	frappe.response["filecontent"] = buf.getvalue()
	frappe.response["type"] = "binary"


def _build(from_s, to_s, as_of_s, keys, bl, monthly, account_id):
	company = _company()
	from_d, to_d = _resolve_period(from_s, to_s)
	# asOf is honoured when passed and defaults to the period end. The Financial
	# Reports screen drives Neraca from its own as-of picker, so without this the
	# exported balance sheet would silently disagree with the screen whenever
	# that picker had been touched.
	as_of_d = _parse_date(as_of_s).date() if as_of_s else to_d
	monthly = 1 if monthly else 0

	ctx = {
		"company": company,
		"from_d": from_d,
		"to_d": to_d,
		"as_of_d": as_of_d,
		"bl": bl,
		"monthly": monthly,
		"account_id": account_id,
		"period_label": "Periode %s sampai %s" % (_date_id(str(from_d)), _date_id(str(to_d))),
		"bl_label": "Lini Bisnis: %s" % BL_LABEL.get(bl, bl),
		"rows": _account_rows(company),
	}

	specs = []
	checks = []
	for key in keys:
		spec, check = _BUILDERS[key](ctx)
		if spec:
			specs.append(spec)
		if check:
			checks.append(check)

	specs.insert(0, _cover_sheet(ctx, keys, checks))
	meta = {"brand": frappe.db.get_value("Company", company, "company_name") or company}
	# Palette and marks follow the business line: Ratunda purple, Poiesis pink,
	# and a neutral blue carrying both marks when the run covers the company.
	theme = theme_for(bl)
	logos = [
		path
		for path in (
			frappe.get_app_path("orion", "public", "images", name)
			for name in theme["logos"]
		)
		if os.path.exists(path)
	]
	wb = build_workbook(specs, meta, logos, theme)

	stamp = "%s_%s" % (from_d.strftime("%Y%m"), to_d.strftime("%Y%m"))
	name = "Laporan-Keuangan-%s.xlsx" % stamp
	if len(keys) == 1:
		name = "%s-%s.xlsx" % (keys[0], stamp)
	return wb, name


def _resolve_period(from_s, to_s):
	"""Period bounds, defaulting to the current year to date."""
	import datetime

	today = datetime.date.today()
	from_d = _parse_date(from_s).date() if from_s else datetime.date(today.year, 1, 1)
	to_d = _parse_date(to_s).date() if to_s else today
	if to_d < from_d:
		frappe.throw("Periode akhir mendahului periode awal")
	return from_d, to_d


# ── shared helpers ──────────────────────────────────────────────────────────


def _month_columns(ctx, label_fn=_month_label, total=True):
	"""Per-month money columns, plus a TOTAL when the report is a flow."""
	from orion.accounting.report_math import month_keys

	months = month_keys(ctx["from_d"], ctx["to_d"])
	cols = [{"header": label_fn(m), "width": 16, "kind": "money"} for m in months]
	if total:
		cols.append({"header": "TOTAL", "width": 18, "kind": "money"})
	return months, cols


def _notes(ctx, *extra):
	out = [ctx["bl_label"], RUPIAH_NOTE]
	out.extend(x for x in extra if x)
	return out


def _check(label, ok, detail=""):
	return {"label": label, "ok": bool(ok), "detail": detail}


# ── 1. Chart of Accounts ────────────────────────────────────────────────────


def _sheet_coa(ctx):
	"""The chart as a master list.

	Deliberately NOT account_tree: that helper promotes away the synthetic
	X-0000 roots because a P&L section strip already names them, whereas a
	chart of accounts must show ASET / KEWAJIBAN / EKUITAS as real rows.
	"""
	rows = [r for r in ctx["rows"] if _bl_ok(r, ctx["bl"])]
	names = {r.name for r in rows}
	by_parent: dict = {}
	roots = []
	for r in rows:
		if r.parent_account and r.parent_account in names:
			by_parent.setdefault(r.parent_account, []).append(r)
		else:
			roots.append(r)

	def code_of(r):
		return r.account_number or ""

	out = []

	def walk(node, depth):
		out.append(
			{
				"style": "section" if node.is_group else "normal",
				"indent": depth,
				"cells": [
					code_of(node),
					node.account_name,
					_orion_type(node),
					node.orion_business_line or "ALL",
					_normal_balance(node.root_type),
					"Grup" if node.is_group else "Detail",
					"Nonaktif" if node.disabled else "Aktif",
				],
			}
		)
		for child in sorted(by_parent.get(node.name, []), key=code_of):
			walk(child, depth + 1)

	for r in sorted(roots, key=code_of):
		walk(r, 0)

	spec = {
		"name": "CoA",
		"title": "DAFTAR AKUN (CHART OF ACCOUNTS)",
		"subtitle": "Posisi struktur akun saat ini",
		"notes": _notes(
			ctx,
			"Daftar akun adalah data induk, tidak terpengaruh periode yang dipilih.",
		),
		"indent_col": 1,
		"columns": [
			{"header": "KODE", "width": 12, "kind": "text"},
			{"header": "NAMA AKUN", "width": 42, "kind": "text"},
			{"header": "TIPE", "width": 16, "kind": "text"},
			{"header": "LINI BISNIS", "width": 18, "kind": "text"},
			{"header": "SALDO NORMAL", "width": 14, "kind": "text"},
			{"header": "LEVEL", "width": 10, "kind": "text"},
			{"header": "STATUS", "width": 11, "kind": "text"},
		],
		"rows": out,
	}
	return spec, _check("Daftar Akun", True, "%d akun" % len(out))


# ── 2. Buku Besar ───────────────────────────────────────────────────────────


def _sheet_ledger(ctx):
	company = ctx["company"]
	if ctx.get("account_id"):
		acc = _resolve_account(ctx["account_id"])
		if not acc:
			frappe.throw("Account not found", exc=frappe.DoesNotExistError)
		accounts = [acc]
	else:
		accounts = [
			r for r in ctx["rows"]
			if not r.disabled and not r.is_group and _bl_ok(r, ctx["bl"])
		]
	blocks = _gl_blocks(company, accounts, ctx["from_d"], ctx["to_d"])

	rows = []
	total_d = 0.0
	total_c = 0.0
	for b in blocks:
		acc = b["account"]
		if not b["rows"] and not b["openingBalance"]:
			continue  # a silent account adds nothing to a ledger listing
		rows.append(
			{
				"style": "subtotal",
				"cells": [
					acc["code"], acc["name"], None, None, "Saldo Awal", None,
					None, None, b["openingBalance"],
				],
			}
		)
		for r in b["rows"]:
			rows.append(
				{
					"cells": [
						acc["code"], acc["name"], r["date"], r["entryId"],
						r["description"], r["sourceType"],
						r["debit"], r["credit"], r["balance"],
					]
				}
			)
		rows.append(
			{
				"style": "subtotal",
				"cells": [
					acc["code"], acc["name"], None, None, "Saldo Akhir", None,
					b["totalDebit"], b["totalCredit"], b["closingBalance"],
				],
			}
		)
		total_d += b["totalDebit"]
		total_c += b["totalCredit"]

	spec = {
		"name": "Buku Besar",
		"title": "BUKU BESAR",
		"subtitle": ctx["period_label"],
		# One flat sheet rather than one tab per account: 89 tabs would also
		# collide with Excel's 31-character sheet-name limit.
		"notes": _notes(ctx, "Saldo berjalan dihitung ulang per akun."),
		"list_shaped": True,
		"columns": [
			{"header": "KODE AKUN", "width": 12, "kind": "text"},
			{"header": "NAMA AKUN", "width": 30, "kind": "text"},
			{"header": "TANGGAL", "width": 12, "kind": "text"},
			{"header": "REFERENSI", "width": 20, "kind": "text"},
			{"header": "URAIAN", "width": 46, "kind": "text"},
			{"header": "SUMBER", "width": 16, "kind": "text"},
			{"header": "DEBIT", "width": 16, "kind": "money"},
			{"header": "KREDIT", "width": 16, "kind": "money"},
			{"header": "SALDO", "width": 18, "kind": "money"},
		],
		"rows": rows,
	}
	ok = abs(total_d - total_c) < 1.0
	return spec, _check(
		"Buku Besar",
		ok,
		"Debit %s vs Kredit %s" % (_fmt(total_d), _fmt(total_c)),
	)


# ── 3. Neraca Saldo ─────────────────────────────────────────────────────────


def _sheet_trial_balance(ctx):
	company, bl = ctx["company"], ctx["bl"]
	accounts = _tb_accounts(company, bl)
	period = _aggregate(company, ctx["from_d"], ctx["to_d"])
	report = _trial_balance_from(accounts, period, str(ctx["from_d"]), str(ctx["to_d"]))

	if ctx["monthly"]:
		months, mcols = _month_columns(ctx)
		per_month = _aggregate_monthly(company, ctx["from_d"], ctx["to_d"])
		monthly_reports = {
			m: _trial_balance_from(accounts, per_month.get(m, {}), "", "") for m in months
		}
		by_code = {
			m: {a["code"]: a for a in rep["accounts"]} for m, rep in monthly_reports.items()
		}
		columns = [
			{"header": "KODE", "width": 12, "kind": "text"},
			{"header": "NAMA AKUN", "width": 38, "kind": "text"},
			*mcols,
		]
		rows = []
		for a in report["accounts"]:
			cells = [a["code"], a["name"]]
			cells += [by_code[m].get(a["code"], {}).get("balance", 0.0) for m in months]
			cells.append(a["balance"])
			rows.append({"cells": cells})
		tot = ["", "TOTAL DEBIT - KREDIT"]
		tot += [
			monthly_reports[m]["totalDebit"] - monthly_reports[m]["totalCredit"]
			for m in months
		]
		tot.append(report["totalDebit"] - report["totalCredit"])
		rows.append({"style": "total", "cells": tot})
	else:
		columns = [
			{"header": "KODE", "width": 12, "kind": "text"},
			{"header": "NAMA AKUN", "width": 42, "kind": "text"},
			{"header": "TIPE", "width": 16, "kind": "text"},
			{"header": "DEBIT", "width": 18, "kind": "money"},
			{"header": "KREDIT", "width": 18, "kind": "money"},
			{"header": "SALDO", "width": 18, "kind": "money"},
		]
		rows = [
			{
				"cells": [
					a["code"], a["name"], a["type"],
					a["totalDebit"], a["totalCredit"], a["balance"],
				]
			}
			for a in report["accounts"]
		]
		rows.append(
			{
				"style": "total",
				"cells": [
					"", "TOTAL", "", report["totalDebit"], report["totalCredit"], None,
				],
			}
		)

	spec = {
		"name": "Neraca Saldo",
		"title": "NERACA SALDO",
		"subtitle": ctx["period_label"],
		"notes": _notes(ctx),
		"list_shaped": not ctx["monthly"],
		"columns": columns,
		"rows": rows,
	}
	return spec, _check(
		"Neraca Saldo",
		report["isBalanced"],
		"Debit %s vs Kredit %s" % (_fmt(report["totalDebit"]), _fmt(report["totalCredit"])),
	)


# ── 4. Laba Rugi ────────────────────────────────────────────────────────────


_PL_SECTIONS = (
	("revenue", "PENDAPATAN", ("REVENUE", "OTHER_INCOME")),
	("cogs", "HARGA POKOK PENJUALAN", ("COGS",)),
	("expenses", "BEBAN OPERASIONAL", ("EXPENSE", "OTHER_EXPENSE")),
)


def _sheet_profit_loss(ctx):
	company, bl = ctx["company"], ctx["bl"]
	accounts, headers, legacy = _pl_accounts(company, bl)
	period = _aggregate(company, ctx["from_d"], ctx["to_d"])
	report = _profit_loss_from(
		accounts, headers, legacy, period, str(ctx["from_d"]), str(ctx["to_d"])
	)

	months, mcols = ([], [])
	monthly_reports = {}
	if ctx["monthly"]:
		months, mcols = _month_columns(ctx)
		per_month = _aggregate_monthly(company, ctx["from_d"], ctx["to_d"])
		monthly_reports = {
			m: _profit_loss_from(accounts, headers, legacy, per_month.get(m, {}), "", "")
			for m in months
		}

	columns = [
		{"header": "KODE", "width": 12, "kind": "text"},
		{"header": "NAMA AKUN", "width": 42, "kind": "text"},
	]
	columns += mcols if ctx["monthly"] else [{"header": "JUMLAH", "width": 20, "kind": "money"}]

	rows = []
	for key, label, types in _PL_SECTIONS:
		rows.append({"style": "section", "cells": ["", label]})
		# The tree rollup is why header rows show subtotals rather than zero:
		# a group account's own balance is direct GL only, normally nil.
		tree = flatten_tree(account_tree(report[key]["accounts"], report["headers"], types))
		month_trees = {
			m: {
				n["code"]: n["amount"]
				for n in flatten_tree(
					account_tree(monthly_reports[m][key]["accounts"], monthly_reports[m]["headers"], types)
				)
			}
			for m in months
		}
		for node in tree:
			cells = [node["code"], node["name"]]
			if ctx["monthly"]:
				cells += [month_trees[m].get(node["code"], 0.0) for m in months]
				cells.append(node["amount"])
			else:
				cells.append(node["amount"])
			rows.append({"cells": cells, "indent": node["depth"]})
		rows.append(
			{
				"style": "subtotal",
				"cells": _line(
					"", "Total %s" % label.title(), report[key]["total"],
					months, lambda m, k=key: monthly_reports[m][k]["total"], ctx,
				),
			}
		)
		rows.append({"style": "spacer", "cells": []})

	rows.append(
		{
			"style": "subtotal",
			"cells": _line(
				"", "LABA KOTOR", report["grossProfit"], months,
				lambda m: monthly_reports[m]["grossProfit"], ctx,
			),
		}
	)
	rows.append(
		{
			"style": "total",
			"cells": _line(
				"", "LABA BERSIH", report["netIncome"], months,
				lambda m: monthly_reports[m]["netIncome"], ctx,
			),
		}
	)

	spec = {
		"name": "Laba Rugi",
		"title": "LAPORAN LABA RUGI",
		"subtitle": ctx["period_label"],
		"notes": _notes(ctx),
		"indent_col": 1,
		"columns": columns,
		"rows": rows,
	}
	return spec, _check("Laba Rugi", True, "Laba bersih %s" % _fmt(report["netIncome"]))


def _line(code, label, total, months, month_fn, ctx):
	cells = [code, label]
	if ctx["monthly"]:
		cells += [month_fn(m) for m in months]
	cells.append(total)
	return cells


# ── 5. Neraca ───────────────────────────────────────────────────────────────


def _sheet_balance_sheet(ctx):
	company, bl = ctx["company"], ctx["bl"]
	accounts, legacy = _bs_accounts(company, bl)
	aggs = _aggregate(company, None, ctx["as_of_d"])
	report = _balance_sheet_from(accounts, legacy, aggs, str(ctx["as_of_d"]))

	months, mcols = ([], [])
	monthly_reports = {}
	if ctx["monthly"]:
		# Stock columns are as-of snapshots, so they are labelled "per <date>"
		# and get NO total column — a sum of balances is meaningless.
		months, mcols = _month_columns(ctx, label_fn=_month_end_label, total=False)
		per_month = _aggregate_monthly(company, ctx["from_d"], ctx["to_d"], cumulative=True)
		monthly_reports = {
			m: _balance_sheet_from(accounts, legacy, per_month.get(m, {}), "") for m in months
		}

	columns = [
		{"header": "KODE", "width": 12, "kind": "text"},
		{"header": "NAMA AKUN", "width": 42, "kind": "text"},
	]
	columns += mcols if ctx["monthly"] else [{"header": "SALDO", "width": 20, "kind": "money"}]

	rows = []
	for key, label in (("assets", "ASET"), ("liabilities", "KEWAJIBAN"), ("equity", "EKUITAS")):
		rows.append({"style": "section", "cells": ["", label]})
		for a in report[key]["accounts"]:
			cells = [a["code"], a["name"]]
			if ctx["monthly"]:
				cells += [
					_find_balance(monthly_reports[m][key]["accounts"], a["code"]) for m in months
				]
			else:
				cells.append(a["balance"])
			rows.append({"cells": cells, "indent": 1})
		rows.append(
			{
				"style": "subtotal",
				"cells": _line(
					"", "Total %s" % label.title(), report[key]["total"], months,
					lambda m, k=key: monthly_reports[m][k]["total"], ctx,
				),
			}
		)
		rows.append({"style": "spacer", "cells": []})

	rows.append(
		{
			"style": "subtotal",
			"cells": _line(
				"", "KEWAJIBAN + EKUITAS", report["totalLiabilitiesAndEquity"], months,
				lambda m: monthly_reports[m]["totalLiabilitiesAndEquity"], ctx,
			),
		}
	)

	# The balance sheet is structurally unbalanced until closing entries exist —
	# there is no synthetic retained-earnings row, by design (see
	# _balance_sheet's docstring). Rather than leave the reader to discover that,
	# show the gap explicitly and check it against profit for the same window.
	gap = report["assets"]["total"] - report["totalLiabilitiesAndEquity"]
	rows.append(
		{
			"style": "flag" if abs(gap) >= 1.0 else "ok",
			"cells": _line(
				"", "Selisih (laba berjalan belum ditutup)", gap, months,
				lambda m: monthly_reports[m]["assets"]["total"]
				- monthly_reports[m]["totalLiabilitiesAndEquity"],
				ctx,
			),
		}
	)
	unclosed = _unclosed_profit(ctx)
	matched = abs(gap - unclosed) < 1.0
	if abs(gap) >= 1.0:
		rows.append(
			{
				"style": "ok" if matched else "info",
				"cells": [
					"",
					"Laba bersih kumulatif (belum ditutup ke ekuitas): %s%s"
					% (_fmt(unclosed), ", cocok dengan selisih di atas" if matched else ""),
				],
			}
		)

	spec = {
		"name": "Neraca",
		"title": "NERACA",
		"subtitle": "Per %s" % _date_id(str(ctx["as_of_d"])),
		"notes": _notes(ctx),
		"indent_col": 1,
		"columns": columns,
		"rows": rows,
	}
	return spec, _check(
		"Neraca",
		abs(gap) < 1.0 or matched,
		"Selisih %s = laba kumulatif belum ditutup %s" % (_fmt(gap), _fmt(unclosed)),
	)


def _find_balance(accounts, code):
	for a in accounts:
		if a["code"] == code:
			return a["balance"]
	return 0.0


def _unclosed_profit(ctx):
	"""Cumulative profit from inception to asOf.

	This is exactly what the balance-sheet gap equals while no closing entries
	have been posted — verified on live data at 2026-09-30, where both come to
	-326.988.671,21 to the rupiah. Note it is cumulative, NOT year-to-date: the
	gap carries every prior year's unclosed result too, so comparing against
	this year alone would be off by the earlier years (about 17 juta here)."""
	company, bl = ctx["company"], ctx["bl"]
	accounts, headers, legacy = _pl_accounts(company, bl)
	aggs = _aggregate(company, None, ctx["as_of_d"])
	return _profit_loss_from(accounts, headers, legacy, aggs, "", "")["netIncome"]


# ── 6. Arus Kas ─────────────────────────────────────────────────────────────


def _sheet_cash_flow(ctx):
	data = _cash_flow({"from": str(ctx["from_d"]), "to": str(ctx["to_d"])})
	months = data["months"] if ctx["monthly"] else []

	columns = [
		{"header": "KODE", "width": 12, "kind": "text"},
		{"header": "KETERANGAN", "width": 46, "kind": "text"},
	]
	if ctx["monthly"]:
		columns += [{"header": _month_label(m), "width": 16, "kind": "money"} for m in months]
		columns.append({"header": "TOTAL", "width": 18, "kind": "money"})
	else:
		columns += [
			{"header": "MASUK", "width": 18, "kind": "money"},
			{"header": "KELUAR", "width": 18, "kind": "money"},
			{"header": "NETO", "width": 18, "kind": "money"},
		]

	def row(code, label, net, inflow=None, outflow=None, by_month=None, style=None, indent=0):
		cells = [code, label]
		if ctx["monthly"]:
			cells += [(by_month or {}).get(m, 0.0) for m in months]
			cells.append(net)
		else:
			cells += [inflow, outflow, net]
		return {"cells": cells, "style": style, "indent": indent}

	# Saldo Awal / Saldo Akhir are positions, not flows: in monthly mode each
	# column carries that month's own opening/closing balance, so the row reads
	# as a running position. Its last column is therefore the period's closing
	# figure, not a sum of the columns — which is why the sheet says so.
	rows = [
		row("", "SALDO AWAL KAS & BANK", data["saldoAwal"], None, None,
		    {m: data["byMonth"][m]["saldoAwal"] for m in months}, "subtotal")
	]
	rows.append({"style": "spacer", "cells": []})

	for section in data["sections"]:
		rows.append({"style": "section", "cells": ["", section["label"]]})
		for a in section["accounts"]:
			rows.append(
				row(a["code"], a["name"], a["net"], a["inflow"], a["outflow"],
				    a["by_month"], None, 1)
			)
		rows.append(
			row("", "Jumlah %s" % section["label"], section["total"], None, None,
			    section["byMonth"], "subtotal")
		)
		rows.append({"style": "spacer", "cells": []})

	if data["suspense"]["net"]:
		rows.append(
			row(data["suspense"]["code"], "Pos Sementara — Rekonsiliasi Bank (perlu ditelusuri)",
			    data["suspense"]["net"], None, None, data["suspense"]["byMonth"], "flag")
		)
		rows.append({"style": "spacer", "cells": []})

	rows.append(row("", "MUTASI BERSIH KAS", data["mutasiBersih"], None, None,
	                {m: data["byMonth"][m]["neto"] for m in months}, "subtotal"))
	rows.append(row("", "SALDO AKHIR KAS & BANK", data["saldoAkhir"], None, None,
	                {m: data["byMonth"][m]["saldoAkhir"] for m in months}, "total"))

	# Independent tie-out, always shown — never plugged silently.
	rows.append({"style": "spacer", "cells": []})
	rows.append(row("", "Saldo akhir menurut Buku Besar", data["saldoAkhirBukuBesar"],
	                None, None, None, "normal"))
	rows.append(
		row("", "SELISIH BELUM TERJELASKAN" if not data["isBalanced"] else "Selisih (nihil)",
		    data["selisih"], None, None, None,
		    "ok" if data["isBalanced"] else "flag")
	)

	# Per-bank movement: a second, independent route to the same closing figure,
	# and the block that gets held against six bank statements.
	rows.append({"style": "spacer", "cells": []})
	rows.append({"style": "section", "cells": ["", "MUTASI PER REKENING"]})
	for a in data["perAccount"]:
		cells = [a["code"], a["name"]]
		if ctx["monthly"]:
			cells += [None] * len(months) + [a["mutasi"]]
		else:
			cells += [a["debit"], -a["kredit"], a["mutasi"]]
		rows.append({"cells": cells, "indent": 1})

	if data["transfers"]:
		rows.append({"style": "spacer", "cells": []})
		rows.append({"style": "section", "cells": ["", "MUTASI ANTAR KAS (tidak mempengaruhi arus kas)"]})
		for t in data["transfers"]:
			cells = [t["code"], t["name"]]
			if ctx["monthly"]:
				cells += [None] * len(months) + [t["debit"] - t["kredit"]]
			else:
				cells += [t["debit"], -t["kredit"], t["debit"] - t["kredit"]]
			rows.append({"cells": cells, "indent": 1})
		rows.append(
			row("", "Jumlah (harus nol)", data["transfersTotal"], None, None, None,
			    "ok" if abs(data["transfersTotal"]) < 1.0 else "flag")
		)

	for u in data["unbalancedVouchers"][:20]:
		rows.append(
			{"style": "flag", "cells": ["", "Voucher tidak seimbang: %s %s (selisih %s)"
			 % (u["voucherType"], u["voucherNo"], _fmt(u["selisih"]))]}
		)
	for o in data["outsidePeriod"][:20]:
		rows.append(
			{"style": "flag", "cells": ["", "Baris di luar periode: %s %s (%s)"
			 % (o["voucherType"], o["voucherNo"], o["account"])]}
		)

	spec = {
		"name": "Arus Kas",
		"title": "LAPORAN ARUS KAS (METODE LANGSUNG)",
		"subtitle": ctx["period_label"],
		"notes": [
			data["businessLineNote"],
			RUPIAH_NOTE,
			"Kas & bank: " + ", ".join(a["code"] for a in data["cashAccounts"]),
		] + (
			["Baris Saldo Awal dan Saldo Akhir menunjukkan posisi tiap bulan, "
			 "bukan penjumlahan kolom."]
			if ctx["monthly"] else []
		),
		"indent_col": 1,
		"columns": columns,
		"rows": rows,
	}
	return spec, _check(
		"Arus Kas", data["isBalanced"],
		"Selisih %s" % _fmt(data["selisih"]),
	)


# ── 7. Jurnal Umum ──────────────────────────────────────────────────────────


def _sheet_journal(ctx):
	company = ctx["company"]
	# Sourced from GL, not from Journal Entry: Payment Entries and Sales Invoices
	# are ~7% of live GL rows, and a Journal-Entry-only listing would not tie out
	# to Neraca Saldo.
	gles = frappe.get_all(
		"GL Entry",
		filters=[
			["company", "=", company],
			["is_cancelled", "=", 0],
			["posting_date", ">=", ctx["from_d"]],
			["posting_date", "<=", ctx["to_d"]],
		],
		fields=[
			"posting_date", "voucher_type", "voucher_no", "account",
			"debit", "credit", "remarks", "project",
		],
		order_by="posting_date asc, voucher_no asc, creation asc",
	)
	by_name = {r.name: r for r in ctx["rows"]}

	rows = []
	total_d = 0.0
	total_c = 0.0
	for g in gles:
		acc = by_name.get(g.account)
		d = float(g.debit or 0)
		c = float(g.credit or 0)
		total_d += d
		total_c += c
		note = g.remarks if g.remarks and g.remarks != "No Remarks" else None
		rows.append(
			{
				"cells": [
					str(g.posting_date), g.voucher_no, g.voucher_type,
					(acc.account_number or "") if acc else "",
					acc.account_name if acc else g.account,
					note, g.project or "", d, c,
				]
			}
		)
	rows.append(
		{"style": "total", "cells": ["", "TOTAL", "", "", "", "", "", total_d, total_c]}
	)

	spec = {
		"name": "Jurnal Umum",
		"title": "JURNAL UMUM",
		"subtitle": ctx["period_label"],
		"notes": [
			RUPIAH_NOTE,
			"Mencakup seluruh tipe voucher (Journal Entry, Payment Entry, Sales Invoice).",
		],
		"list_shaped": True,
		"columns": [
			{"header": "TANGGAL", "width": 12, "kind": "text"},
			{"header": "VOUCHER", "width": 22, "kind": "text"},
			{"header": "TIPE", "width": 16, "kind": "text"},
			{"header": "KODE AKUN", "width": 12, "kind": "text"},
			{"header": "NAMA AKUN", "width": 34, "kind": "text"},
			{"header": "URAIAN", "width": 46, "kind": "text"},
			{"header": "PROYEK", "width": 16, "kind": "text"},
			{"header": "DEBIT", "width": 16, "kind": "money"},
			{"header": "KREDIT", "width": 16, "kind": "money"},
		],
		"rows": rows,
	}
	return spec, _check(
		"Jurnal Umum", abs(total_d - total_c) < 1.0,
		"%d baris, Debit %s vs Kredit %s" % (len(gles), _fmt(total_d), _fmt(total_c)),
	)


# ── 8. Piutang & DP Klien ───────────────────────────────────────────────────


def _sheet_ar(ctx):
	company = ctx["company"]
	as_of = ctx["as_of_d"]
	invoices = frappe.get_all(
		"Sales Invoice",
		filters=[["company", "=", company], ["docstatus", "=", 1],
		         ["posting_date", "<=", as_of]],
		fields=["name", "customer", "posting_date", "due_date", "grand_total",
		        "outstanding_amount", "project"],
		order_by="posting_date asc",
	)
	paid = _si_allocations([i.name for i in invoices], as_of)

	rows = []
	total_out = 0.0
	for i in invoices:
		total = float(i.grand_total or 0)
		settled = paid.get(i.name, 0.0)
		outstanding = total - settled
		if abs(outstanding) < 0.005:
			continue
		days = (as_of - i.due_date).days if i.due_date else 0
		total_out += outstanding
		rows.append(
			{
				"cells": [
					i.name, i.customer, i.project or "",
					str(i.posting_date), str(i.due_date or ""),
					aging_bucket(days), total, settled, outstanding,
				]
			}
		)
	if not rows:
		# An empty body here is a finding, not a formatting accident: every
		# invoice is marked settled at document level. Say so, rather than
		# leaving the reader to wonder whether the export failed.
		rows.append(
			{
				"style": "info",
				"cells": [
					"",
					"Tidak ada invoice dengan sisa tagihan per tanggal ini — seluruh "
					"invoice tercatat lunas di tingkat dokumen. Bandingkan dengan "
					"saldo Buku Besar di bawah.",
				],
			}
		)
	rows.append(
		{"style": "total", "cells": ["", "TOTAL PIUTANG (dari invoice)", "", "", "", "",
		                             None, None, total_out]}
	)

	# The invoice layer and the GL layer disagree here, so show both rather than
	# publish one and let the reader assume it is the whole story.
	gl_ar = _gl_balance(company, "1-12", as_of)
	gl_dp = -_gl_balance(company, "2-12", as_of)
	diff = total_out - gl_ar
	rows.append({"style": "spacer", "cells": []})
	rows.append({"style": "section", "cells": ["", "REKONSILIASI DENGAN BUKU BESAR"]})
	rows.append({"cells": ["1-12xx", "Piutang Usaha menurut Buku Besar", "", "", "", "",
	                       None, None, gl_ar]})
	rows.append({"cells": ["2-12xx", "DP & Pelunasan Klien (uang muka diterima)", "", "", "", "",
	                       None, None, gl_dp]})
	rows.append(
		{
			"style": "flag" if abs(diff) >= 1.0 else "ok",
			"cells": ["", "Selisih invoice vs Buku Besar", "", "", "", "", None, None, diff],
		}
	)
	rows.append(
		{
			"style": "info",
			"cells": [
				"",
				"Pembayaran klien dibukukan ke Buku Besar tetapi tidak selalu "
				"menutup invoice, sehingga kedua angka di atas dapat berbeda.",
			],
		}
	)

	spec = {
		"name": "Piutang",
		"title": "PIUTANG & DP KLIEN",
		"subtitle": "Per %s" % _date_id(str(as_of)),
		"notes": [
			RUPIAH_NOTE,
			"Sisa tagihan dihitung per tanggal laporan, bukan posisi hari ini.",
		],
		"list_shaped": False,
		"columns": [
			{"header": "INVOICE", "width": 20, "kind": "text"},
			{"header": "KLIEN", "width": 30, "kind": "text"},
			{"header": "PROYEK", "width": 14, "kind": "text"},
			{"header": "TANGGAL", "width": 12, "kind": "text"},
			{"header": "JATUH TEMPO", "width": 13, "kind": "text"},
			{"header": "UMUR", "width": 17, "kind": "text"},
			{"header": "NILAI", "width": 17, "kind": "money"},
			{"header": "DIBAYAR", "width": 17, "kind": "money"},
			{"header": "SISA", "width": 17, "kind": "money"},
		],
		"rows": rows,
	}
	return spec, _check(
		"Piutang", abs(diff) < 1.0,
		"Invoice %s vs Buku Besar %s" % (_fmt(total_out), _fmt(gl_ar)),
	)


def _si_allocations(si_names, as_of):
	"""Amount applied to each invoice on or before as_of.

	Uses per.allocated_amount — the share applied to THIS invoice — rather than
	pe.paid_amount, the whole payment. _si_payments in accounting_reports.py
	reads paid_amount; that is harmless today because every Payment Entry
	references exactly one invoice, but it would overstate each one the first
	time a combined payment is recorded, so the export does not inherit it.
	"""
	out: dict = {}
	if not si_names:
		return out
	names = tuple(si_names)
	pes = frappe.db.sql(
		"""select per.reference_name as si, sum(per.allocated_amount) as amt
		from `tabPayment Entry Reference` per
		join `tabPayment Entry` pe on pe.name = per.parent
		where pe.docstatus = 1 and per.reference_doctype = 'Sales Invoice'
			and per.reference_name in %(names)s and pe.posting_date <= %(as_of)s
		group by per.reference_name""",
		{"names": names, "as_of": as_of},
		as_dict=True,
	)
	for p in pes:
		out[p.si] = out.get(p.si, 0.0) + float(p.amt or 0)
	jes = frappe.db.sql(
		"""select jea.reference_name as si,
			sum(jea.credit_in_account_currency) as amt
		from `tabJournal Entry Account` jea
		join `tabJournal Entry` je on je.name = jea.parent
		where je.docstatus = 1 and jea.reference_type = 'Sales Invoice'
			and jea.reference_name in %(names)s and je.posting_date <= %(as_of)s
			and jea.credit_in_account_currency > 0
		group by jea.reference_name""",
		{"names": names, "as_of": as_of},
		as_dict=True,
	)
	for p in jes:
		out[p.si] = out.get(p.si, 0.0) + float(p.amt or 0)
	return out


def _gl_balance(company, code_prefix, as_of) -> float:
	"""Signed debit - credit over every account whose code starts with prefix."""
	row = frappe.db.sql(
		"""select coalesce(sum(g.debit), 0), coalesce(sum(g.credit), 0)
		from `tabGL Entry` g
		join `tabAccount` a on a.name = g.account
		where g.company = %s and g.is_cancelled = 0 and g.posting_date <= %s
			and a.account_number like %s""",
		(company, as_of, code_prefix + "%"),
	)[0]
	return float(row[0]) - float(row[1])


# ── cover ───────────────────────────────────────────────────────────────────


def _cover_sheet(ctx, keys, checks):
	"""RINGKASAN — what was asked for, and whether each sheet ties out."""
	rows = [
		{"style": "section", "cells": ["PARAMETER", "", ""]},
		{"cells": ["Periode", ctx["period_label"], ""]},
		{"cells": ["Per tanggal (Neraca, Piutang)", _date_id(str(ctx["as_of_d"])), ""]},
		{"cells": ["Lini bisnis", BL_LABEL.get(ctx["bl"], ctx["bl"]), ""]},
		{"cells": ["Rincian per bulan", "Ya" if ctx["monthly"] else "Tidak", ""]},
		{"cells": ["Dibuat oleh", frappe.session.user, ""]},
		{"cells": ["Dibuat pada", now_datetime().strftime("%d-%m-%Y %H:%M"), ""]},
		{"style": "spacer", "cells": []},
		{"style": "section", "cells": ["PEMERIKSAAN", "KETERANGAN", "STATUS"]},
	]
	for c in checks:
		rows.append(
			{
				"style": "ok" if c["ok"] else "flag",
				"cells": [c["label"], c["detail"], "SEIMBANG" if c["ok"] else "PERIKSA"],
			}
		)
	rows.append({"style": "spacer", "cells": []})
	rows.append(
		{
			"style": "info",
			"cells": [
				"",
				"Status PERIKSA bukan berarti laporan salah — lihat baris bertanda "
				"merah pada sheet terkait untuk rinciannya.",
				"",
			],
		}
	)
	return {
		"name": "RINGKASAN",
		"title": "RINGKASAN EKSPOR LAPORAN KEUANGAN",
		"subtitle": ctx["period_label"],
		"notes": ["Sheet: " + ", ".join(keys)],
		"columns": [
			{"header": "ITEM", "width": 32, "kind": "text"},
			{"header": "KETERANGAN", "width": 60, "kind": "text"},
			{"header": "STATUS", "width": 14, "kind": "text"},
		],
		"rows": rows,
	}


def _fmt(n) -> str:
	try:
		return "Rp {:,.0f}".format(float(n)).replace(",", ".")
	except (TypeError, ValueError):
		return "-"


_BUILDERS = {
	"coa": _sheet_coa,
	"ledger": _sheet_ledger,
	"trial-balance": _sheet_trial_balance,
	"profit-loss": _sheet_profit_loss,
	"balance-sheet": _sheet_balance_sheet,
	"cash-flow": _sheet_cash_flow,
	"journal": _sheet_journal,
	"ar": _sheet_ar,
}

__all__ = ["MONEY_FMT", "export_xlsx"]
