"""Pure report arithmetic behind the accounting Excel export.

Deliberately frappe-free, like its siblings in this package (txn_hash,
loan_schedule, same_account_dedup): every function takes plain dicts and lists
and returns the same, so all of it is exercisable with

    python3 -m unittest discover orion/orion/accounting/tests

without a bench. The frappe glue — SQL, Orion Settings, the HTTP routes — stays
in orion/compat/accounting.py and orion/compat/finance_export.py.

This is where the arithmetic that must be *right* lives: monthly bucketing,
the CoA tree rollup, and the cash-flow statement.
"""

PL_TYPES = ("REVENUE", "COGS", "EXPENSE", "OTHER_INCOME", "OTHER_EXPENSE")

# Cash-flow sections, in presentation order. LAIN-LAIN is a sink that must stay
# empty in practice; it exists so an unmappable account degrades loudly into a
# visible row instead of silently vanishing and breaking the tie-out.
SECTION_OPERASI = "OPERASI"
SECTION_INVESTASI = "INVESTASI"
SECTION_PENDANAAN = "PENDANAAN"
SECTION_OTHER = "LAIN-LAIN"

SECTION_ORDER = (SECTION_OPERASI, SECTION_INVESTASI, SECTION_PENDANAAN, SECTION_OTHER)

SECTION_LABEL = {
	SECTION_OPERASI: "Arus Kas dari Aktivitas Operasi",
	SECTION_INVESTASI: "Arus Kas dari Aktivitas Investasi",
	SECTION_PENDANAAN: "Arus Kas dari Aktivitas Pendanaan",
	SECTION_OTHER: "Lain-lain (belum terklasifikasi)",
}

# Account whose movement is a bookkeeping staging post, not a cash-flow fact.
# A nonzero period movement here is a defect to clear, so it gets its own
# flagged line rather than being folded into working capital.
SUSPENSE_PREFIX = "1-1900"


# ── month bucketing ─────────────────────────────────────────────────────────


def month_keys(from_date, to_date) -> list:
	"""Contiguous ym keys over an inclusive range: [202601, 202602, ... 202609].

	ym is year * 100 + month, an int that sorts naturally. Accepts anything with
	.year / .month (datetime.date, or a test stub).

	Callers must iterate THIS rather than the ym values present in the data —
	that is what makes a month with no activity carry the previous balance
	forward instead of dropping out of the columns.
	"""
	ym = from_date.year * 100 + from_date.month
	last = to_date.year * 100 + to_date.month
	out = []
	while ym <= last:
		out.append(ym)
		y, m = divmod(ym, 100)
		ym = (y + 1) * 100 + 1 if m == 12 else y * 100 + m + 1
	return out


def fold_monthly(deltas: dict, months: list, cumulative: bool = False) -> dict:
	"""Pivot per-(ym, account) GL deltas into one aggs dict per month.

	deltas: {ym: {account: (debit, credit)}} straight from a `group by account,
	        ym` query — may contain months outside `months`.
	months: the contiguous ym list of the requested period.

	Flow mode returns each month's own activity (for trial balance and P&L).
	Cumulative mode folds everything before the period into a seed and then
	carries balances forward, so each month's value is the as-of-month-end
	position (for the balance sheet).

	The .get default below applies to the per-month DELTA, never to the
	cumulative. Defaulting the cumulative instead would zero out every month in
	which an account happened to have no activity — the classic bug here.
	"""
	if not cumulative:
		return {ym: dict(deltas.get(ym, {})) for ym in months}

	running: dict = {}
	first = months[0] if months else 0
	for ym in sorted(deltas):
		if ym >= first:
			break
		for acct, (d, c) in deltas[ym].items():
			pd, pc = running.get(acct, (0.0, 0.0))
			running[acct] = (pd + d, pc + c)

	out = {}
	for ym in months:
		for acct, (d, c) in deltas.get(ym, {}).items():
			pd, pc = running.get(acct, (0.0, 0.0))
			running[acct] = (pd + d, pc + c)
		out[ym] = dict(running)  # snapshot — later months must not mutate it
	return out


# ── CoA tree ────────────────────────────────────────────────────────────────


def account_tree(leaves: list, headers: list, types) -> list:
	"""Port of buildSectionTree in feynman's FinancialReports.vue — the CoA tree
	the SPA assembles in the browser, needed server-side for the workbook.

	Why this cannot just read a header's `balance`: the P&L endpoint emits header
	rows whose balance is GL posted *directly* to the group account, which is
	normally zero. The subtree rollup exists only in the browser, so writing
	header rows straight from balance would make every header read 0.

	Two deliberate quirks of the original are reproduced rather than fixed, so
	the workbook matches the screen:

	- a header typed differently from its children orphans them (they become
	  roots of their own);
	- promotion drops the synthetic X-0000 roots and splices in their children,
	  discarding those roots' own direct activity. Harmless — group accounts
	  carry no direct GL — and it keeps the tree consistent with the
	  leaves-only section total.

	Returns nested dicts: {code, name, amount, branch, children}.
	"""
	rows = [h for h in headers if h.get("type") in types] + list(leaves)
	nodes = {}
	for a in rows:
		nodes[a["id"]] = {
			"code": a.get("code") or "",
			"name": a.get("name") or "",
			"amount": a.get("balance") or 0.0,
			"branch": False,
			"children": [],
			"_parentId": a.get("parentId"),
			"_isHeader": bool(a.get("isHeader")),
		}

	roots = []
	for n in nodes.values():
		pid = n["_parentId"]
		if pid and pid in nodes:
			parent = nodes[pid]
			parent["children"].append(n)
			parent["branch"] = True
		else:
			roots.append(n)

	def rollup(n):
		# Additive: a non-header parent (6-1600 with child 6-1602) keeps its own
		# activity plus its children's.
		n["amount"] += sum(rollup(c) for c in n["children"])
		return n["amount"]

	for n in roots:
		rollup(n)

	promoted = []
	for n in roots:
		if n["_isHeader"] and not n["_parentId"]:
			promoted.extend(n["children"])
		else:
			promoted.append(n)

	def sort_rec(ns):
		ns.sort(key=lambda x: x["code"])
		for x in ns:
			sort_rec(x["children"])

	sort_rec(promoted)
	return promoted


def flatten_tree(nodes: list, depth: int = 0) -> list:
	"""Depth-tagged preorder walk, for indenting spreadsheet rows."""
	out = []
	for n in nodes:
		out.append({**{k: v for k, v in n.items() if k != "children"}, "depth": depth})
		out.extend(flatten_tree(n["children"], depth + 1))
	return out


# ── cash flow ───────────────────────────────────────────────────────────────


def cash_section(code: str, root_type: str, account_type: str, orion_type: str) -> str:
	"""Which cash-flow section a counterpart account belongs to.

	Derived from root_type / orion_type rather than raw code prefixes wherever
	possible: prefix matching fails *open*, so a newly added account with an
	unmapped prefix would silently vanish and the tie-out would break for a
	code reason indistinguishable from a data problem. Because root_type is
	always one of five values, every account lands somewhere.

	Order matters — first match wins.
	"""
	code = code or ""
	# Loans and owner capital are financing even though they are current
	# liabilities: 2-15xx owner loans, 2-1700 bank, 2-1800 third party.
	if code.startswith("2-15") or code.startswith("2-17") or code.startswith("2-18"):
		return SECTION_PENDANAAN
	if root_type == "Equity":
		return SECTION_PENDANAAN
	if root_type == "Asset" and (
		account_type in ("Fixed Asset", "Accumulated Depreciation") or code.startswith("1-2")
	):
		return SECTION_INVESTASI
	if orion_type in PL_TYPES:
		return SECTION_OPERASI
	# Remaining current assets and liabilities are working capital: client DP
	# (2-121x/2-122x), taxes (2-13xx), project advances (1-131x), payables.
	if root_type in ("Asset", "Liability"):
		return SECTION_OPERASI
	return SECTION_OTHER


def cash_flow(legs, cash_accounts, account_meta, months, opening: float) -> dict:
	"""Direct-method cash-flow statement from the legs of cash-touching vouchers.

	legs: [{voucher_type, voucher_no, account, debit, credit, ym, in_period}] —
	      ALL legs of every voucher that has at least one cash leg inside the
	      period. `in_period` flags whether that leg's own posting_date fell
	      inside the requested range.
	cash_accounts: set of account names forming the cash pool.
	account_meta: {account: {code, name, root_type, account_type, orion_type}}
	months: contiguous ym list of the period.
	opening: cash-pool balance as of the day before the period.

	Attribution rule: every voucher is balanced, so

	    sum_cash(debit - credit) == sum_noncash(credit - debit)

	Each non-cash leg therefore contributes exactly its own (credit - debit) to
	the cash movement. The attributed lines sum to the cash movement with no
	normalisation, no denominator, and no rounding drift. Pro-rata allocation
	would instead produce fiction on precisely the multi-leg vouchers it looks
	like it was invented for: given

	    Dr Bank 5.000.000 / Dr Biaya Admin 50.000 / Cr Pinjaman 5.050.000

	the honest reading is Pendanaan +5.050.000 and Operasi -50.000, not a
	weighted smear across both.

	A voucher with no non-cash leg (a transfer between two of our own bank
	accounts) contributes exactly 0 and so needs no special exclusion — it is
	reported separately for diagnostics only.
	"""
	by_voucher: dict = {}
	for leg in legs:
		by_voucher.setdefault((leg["voucher_type"], leg["voucher_no"]), []).append(leg)

	# section -> account -> {net, inflow, outflow, by_month}
	sections: dict = {k: {} for k in SECTION_ORDER}
	suspense: dict = {"net": 0.0, "by_month": {}}
	transfers: dict = {}
	outside: list = []
	cash_move: dict = {}  # cash account -> [debit, credit]
	net_by_month: dict = {ym: 0.0 for ym in months}
	unbalanced: list = []

	for (vtype, vno), group in by_voucher.items():
		cash_legs = [x for x in group if x["account"] in cash_accounts]
		other_legs = [x for x in group if x["account"] not in cash_accounts]

		# The voucher must balance across ALL its legs for attribution to hold.
		drift = sum(float(x["debit"] or 0) - float(x["credit"] or 0) for x in group)
		if abs(drift) >= 1.0:
			unbalanced.append({"voucherType": vtype, "voucherNo": vno, "selisih": drift})

		for x in cash_legs:
			if not x.get("in_period", True):
				outside.append({"voucherType": vtype, "voucherNo": vno, "account": x["account"]})
				continue
			slot = cash_move.setdefault(x["account"], [0.0, 0.0])
			slot[0] += float(x["debit"] or 0)
			slot[1] += float(x["credit"] or 0)

		if not other_legs:
			# Pure cash-to-cash transfer: contributes 0 by construction.
			for x in cash_legs:
				slot = transfers.setdefault(x["account"], [0.0, 0.0])
				slot[0] += float(x["debit"] or 0)
				slot[1] += float(x["credit"] or 0)
			continue

		for x in other_legs:
			if not x.get("in_period", True):
				outside.append({"voucherType": vtype, "voucherNo": vno, "account": x["account"]})
				continue
			amount = float(x["credit"] or 0) - float(x["debit"] or 0)
			if amount == 0:
				continue
			meta = account_meta.get(x["account"], {})
			code = meta.get("code") or ""
			ym = x["ym"]

			if code.startswith(SUSPENSE_PREFIX):
				suspense["net"] += amount
				suspense["by_month"][ym] = suspense["by_month"].get(ym, 0.0) + amount
			else:
				key = cash_section(
					code,
					meta.get("root_type"),
					meta.get("account_type"),
					meta.get("orion_type"),
				)
				bucket = sections[key].setdefault(
					x["account"],
					{"code": code, "name": meta.get("name") or x["account"],
					 "net": 0.0, "inflow": 0.0, "outflow": 0.0, "by_month": {}},
				)
				bucket["net"] += amount
				if amount > 0:
					bucket["inflow"] += amount
				else:
					bucket["outflow"] += amount
				bucket["by_month"][ym] = bucket["by_month"].get(ym, 0.0) + amount

			if ym in net_by_month:
				net_by_month[ym] += amount

	out_sections = []
	for key in SECTION_ORDER:
		accounts = sorted(sections[key].values(), key=lambda a: a["code"])
		if not accounts and key == SECTION_OTHER:
			continue
		out_sections.append(
			{
				"key": key,
				"label": SECTION_LABEL[key],
				"accounts": accounts,
				"total": float(sum(a["net"] for a in accounts)),
				"byMonth": {
					ym: float(sum(a["by_month"].get(ym, 0.0) for a in accounts))
					for ym in months
				},
			}
		)

	movement = float(sum(s["total"] for s in out_sections)) + suspense["net"]
	closing = opening + movement

	# Per-cash-account decomposition — an independent second route to the same
	# closing balance, and what the finance team holds against bank statements.
	per_account = []
	for acct, (d, c) in sorted(
		cash_move.items(), key=lambda kv: (account_meta.get(kv[0], {}).get("code") or "")
	):
		meta = account_meta.get(acct, {})
		per_account.append(
			{
				"code": meta.get("code") or "",
				"name": meta.get("name") or acct,
				"debit": d,
				"kredit": c,
				"mutasi": d - c,
			}
		)

	# Running per-month opening/closing. In monthly mode this yields a free
	# invariant: saldoAwal of month n+1 must equal saldoAkhir of month n.
	by_month = {}
	running = opening
	for ym in months:
		start = running
		running += net_by_month.get(ym, 0.0)
		by_month[ym] = {"saldoAwal": start, "neto": net_by_month.get(ym, 0.0), "saldoAkhir": running}

	return {
		"saldoAwal": opening,
		"sections": out_sections,
		"suspense": {
			"code": SUSPENSE_PREFIX,
			"net": suspense["net"],
			"byMonth": suspense["by_month"],
		},
		"mutasiBersih": movement,
		"saldoAkhir": closing,
		"perAccount": per_account,
		"transfers": [
			{
				"code": account_meta.get(a, {}).get("code") or "",
				"name": account_meta.get(a, {}).get("name") or a,
				"debit": v[0],
				"kredit": v[1],
			}
			for a, v in sorted(
				transfers.items(), key=lambda kv: (account_meta.get(kv[0], {}).get("code") or "")
			)
		],
		"transfersTotal": float(sum(v[0] - v[1] for v in transfers.values())),
		"byMonth": by_month,
		"outsidePeriod": outside,
		"unbalancedVouchers": unbalanced,
	}


def aging_bucket(days: int) -> str:
	"""Standard receivable aging bands. days < 0 means not yet due."""
	if days < 0:
		return "Belum jatuh tempo"
	if days <= 30:
		return "0-30 hari"
	if days <= 60:
		return "31-60 hari"
	if days <= 90:
		return "61-90 hari"
	return "> 90 hari"
