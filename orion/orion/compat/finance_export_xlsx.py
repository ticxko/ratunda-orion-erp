"""Styled xlsx renderer for the accounting reports.

Spec-driven rather than hard-wired like its sibling expense_report_xlsx: the
accounting sheets have a *dynamic* column count (one column per month when the
monthly breakdown is on), so a fixed COLS/WIDTHS/HEADERS triple cannot describe
them. finance_export.py assembles a list of sheet specs and this module renders
them.

This module calls no frappe API itself, but it lives in orion.compat and
imports expense_report_xlsx, and `orion/compat/__init__.py` imports frappe — so
unlike orion.accounting.report_math it is NOT importable outside a bench, and
its sibling's "testable without a bench" claim does not actually hold either.
That is deliberate: the brand primitives (purple/lavender palette, borders,
rupiah format, logo anchor) are imported rather than duplicated so both
workbooks stay visually identical and cannot drift. Presentation is verified by
opening the generated file; the arithmetic that must be *right* lives in
orion.accounting.report_math, which is genuinely frappe-free and unit-tested.

_sig_row is NOT reused — it is hard-pinned to columns G..K, and financial
statements carry no signature block.

Sheet spec:
  {
    "name":        tab name, trimmed to Excel's 31-char limit,
    "title":       purple title bar text,
    "subtitle":    period line,
    "notes":       [str] extra lines under the subtitle (filters, caveats),
    "columns":     [{"header", "width", "kind"}] — kind: text|money|date|int,
    "rows":        [{"cells": [...], "style": ..., "indent": int}],
    "indent_col":  which cell index a row's `indent` applies to (default 0) —
                   set to the account-name column so CoA trees step visibly,
    "list_shaped": bool — autofilter + freeze panes, for table-shaped sheets,
    "money_fmt":   optional override of the default grid format,
  }

Row styles:
  normal    plain body row
  section   bold lavender band (a statement section heading)
  subtotal  bold, top border
  total     bold white-on-purple
  flag      red fill — a failed tie-out or a defect to clear
  info      italic grey, no borders (notes inside the body)
  spacer    blank row
"""

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill
from openpyxl.utils import get_column_letter

from orion.compat.expense_report_xlsx import (
	ADDRESS,
	BLACK,
	BOX,
	LAVENDER,
	MONTHS_ID,
	PURPLE,
	RP_FMT,
	THIN,
	WHITE,
	_add_logo,
)

# Grid money format: positive;negative;zero. Parentheses for negatives, an em
# dash for zero (the value stays a real 0 so sums and filters still work, it just
# does not shout), and no "Rp " prefix — a monthly breakdown can run to 20+ money
# columns, so the unit is stated once in the sheet header instead.
MONEY_FMT = '#,##0;(#,##0);"—"'

FONT = "Verdana"
GUTTER_WIDTH = 2  # column A, matching the Laporan Pengeluaran workbook
FIRST_COL = 2  # content starts in column B

RED_FILL = PatternFill("solid", fgColor="FFFFC7CE")
RED_FONT = Font(name=FONT, size=10, bold=True, color="FF9C0006")
GREEN_FONT = Font(name=FONT, size=10, bold=True, color="FF006100")


def _month_label(ym: int) -> str:
	"""202601 -> 'Jan 2026'."""
	year, month = divmod(int(ym), 100)
	return "%s %d" % (MONTHS_ID[month - 1][:3], year)


def _month_end_label(ym: int) -> str:
	"""202601 -> 'per 31 Jan 2026' — balance-sheet columns are snapshots, so
	they are labelled as of a date rather than as a period."""
	import calendar

	year, month = divmod(int(ym), 100)
	last = calendar.monthrange(year, month)[1]
	return "per %d %s %d" % (last, MONTHS_ID[month - 1][:3], year)


def build_workbook(sheets: list, meta: dict, logo_path: str | None = None) -> Workbook:
	"""Render sheet specs into a workbook. meta carries the cover-sheet facts."""
	wb = Workbook()
	wb.remove(wb.active)
	for spec in sheets:
		_build_sheet(wb, spec, meta, logo_path)
	if not wb.sheetnames:  # never hand back a workbook with zero sheets
		wb.create_sheet("KOSONG")
	return wb


def _build_sheet(wb: Workbook, spec: dict, meta: dict, logo_path: str | None):
	columns = spec.get("columns") or []
	ncols = max(len(columns), 1)
	name = (spec.get("name") or "SHEET")[:31]
	ws = wb.create_sheet(name)

	ws.column_dimensions["A"].width = GUTTER_WIDTH
	for i, col in enumerate(columns):
		ws.column_dimensions[get_column_letter(FIRST_COL + i)].width = col.get("width", 14)

	last_letter = get_column_letter(FIRST_COL + ncols - 1)
	row = _write_header(ws, spec, meta, ncols, last_letter, logo_path)

	header_row = row
	_write_column_headers(ws, columns, header_row)
	row = header_row + 1

	money_fmt = spec.get("money_fmt") or MONEY_FMT
	indent_col = spec.get("indent_col", 0)
	body_start = row
	for r in spec.get("rows") or []:
		_write_row(ws, r, columns, row, money_fmt, indent_col)
		row += 1
	body_end = row - 1

	# Freeze below the header so column titles stay visible while scrolling.
	ws.freeze_panes = ws.cell(row=header_row + 1, column=FIRST_COL)

	# Autofilter only on table-shaped sheets. On a statement (section bands,
	# spacer rows, subtotals) the region is not a contiguous table, and an
	# autofilter would let the reader hide subtotal rows or mis-range the filter.
	if spec.get("list_shaped") and body_end >= body_start:
		ws.auto_filter.ref = "%s%d:%s%d" % (
			get_column_letter(FIRST_COL), header_row, last_letter, body_end,
		)

	ws.sheet_view.showGridLines = False
	return ws


def _write_header(ws, spec: dict, meta: dict, ncols: int, last_letter: str, logo_path) -> int:
	"""Letterhead, purple title bar, period and notes. Returns the row index the
	column-header band should go on."""
	ws.row_dimensions[1].height = 37
	if logo_path:
		_add_logo(ws, logo_path)

	brand = meta.get("brand") or "PT PENCIPTA ORGANIK INDONESIA"
	ws.cell(row=2, column=FIRST_COL, value=brand).font = Font(
		name=FONT, size=10, bold=True, color=BLACK
	)
	ws.cell(row=3, column=FIRST_COL, value=ADDRESS).font = Font(name="Arial", size=10)
	for col in range(FIRST_COL, FIRST_COL + ncols):
		ws.cell(row=2, column=col).border = Border(bottom=THIN)

	# Purple title bar
	title_row = 5
	ws.row_dimensions[title_row].height = 22
	ws.merge_cells(
		start_row=title_row, start_column=FIRST_COL, end_row=title_row,
		end_column=FIRST_COL + ncols - 1,
	)
	cell = ws.cell(row=title_row, column=FIRST_COL, value=spec.get("title") or "")
	cell.font = Font(name=FONT, size=14, bold=True, color=WHITE)
	cell.fill = PatternFill("solid", fgColor=PURPLE)
	cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)

	row = title_row + 1
	lines = []
	if spec.get("subtitle"):
		lines.append(spec["subtitle"])
	lines.extend(spec.get("notes") or [])
	fill = PatternFill("solid", fgColor=LAVENDER)
	for line in lines:
		ws.merge_cells(
			start_row=row, start_column=FIRST_COL, end_row=row,
			end_column=FIRST_COL + ncols - 1,
		)
		c = ws.cell(row=row, column=FIRST_COL, value=line)
		c.font = Font(name=FONT, size=10, bold=False, color=BLACK)
		c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
		for col in range(FIRST_COL, FIRST_COL + ncols):
			ws.cell(row=row, column=col).fill = fill
		row += 1

	return row + 1  # one blank spacer row before the column headers


def _write_column_headers(ws, columns: list, row: int):
	ws.row_dimensions[row].height = 26
	for i, col in enumerate(columns):
		c = ws.cell(row=row, column=FIRST_COL + i, value=col.get("header") or "")
		c.font = Font(name=FONT, size=10, bold=True, color=WHITE)
		c.fill = PatternFill("solid", fgColor=PURPLE)
		c.border = BOX
		c.alignment = Alignment(
			horizontal="right" if col.get("kind") in ("money", "int") else "left",
			vertical="center",
			wrap_text=True,
		)


def _write_row(ws, spec_row: dict, columns: list, row: int, money_fmt: str, indent_col: int = 0):
	style = spec_row.get("style") or "normal"
	cells = spec_row.get("cells") or []
	indent = spec_row.get("indent") or 0

	if style == "spacer":
		return

	bold = style in ("section", "total", "subtotal", "flag")
	if style == "total":
		font = Font(name=FONT, size=10, bold=True, color=WHITE)
		fill = PatternFill("solid", fgColor=PURPLE)
	elif style == "section":
		font = Font(name=FONT, size=10, bold=True, color=BLACK)
		fill = PatternFill("solid", fgColor=LAVENDER)
	elif style == "flag":
		font = RED_FONT
		fill = RED_FILL
	elif style == "ok":
		font = GREEN_FONT
		fill = None
	elif style == "info":
		font = Font(name=FONT, size=9, italic=True, color="FF595959")
		fill = None
	else:
		font = Font(name=FONT, size=10, bold=bold, color=BLACK)
		fill = None

	bordered = style not in ("info",)

	for i, col in enumerate(columns):
		value = cells[i] if i < len(cells) else None
		c = ws.cell(row=row, column=FIRST_COL + i)
		kind = col.get("kind") or "text"

		if value is not None:
			c.value = value
		if kind == "money" and isinstance(value, (int, float)):
			c.number_format = spec_row.get("money_fmt") or money_fmt
		elif kind == "int" and isinstance(value, (int, float)):
			c.number_format = "#,##0"

		c.font = font
		if fill:
			c.fill = fill
		if bordered:
			c.border = BOX
		c.alignment = Alignment(
			horizontal="right" if kind in ("money", "int") else "left",
			vertical="center",
			indent=(indent if i == indent_col and kind == "text" else 0),
			wrap_text=False,
		)
	if style == "subtotal":
		for i in range(len(columns)):
			cur = ws.cell(row=row, column=FIRST_COL + i)
			cur.border = Border(left=THIN, right=THIN, bottom=THIN, top=THIN)


__all__ = [
	"MONEY_FMT",
	"RP_FMT",
	"_month_end_label",
	"_month_label",
	"build_workbook",
]
