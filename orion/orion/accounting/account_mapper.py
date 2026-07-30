"""Rule-based bank-statement → CoA account suggestion.

Port of `app/services/accounting/account_mapper.py` from Bellatrix Python
(itself a port of `src/lib/accounting/account-mapper.ts`). Regex patterns are
kept in the same order; first match wins.

Frappe adaptation: the source's caller loaded client names from the `clients`
table and passed them in as `client_names`. Here `suggest_accounts` also
accepts an injectable `client_names_provider` callable, defaulting to a
frappe-based Customer lookup that is imported lazily inside the function so
this module stays importable (and testable) without frappe.
"""

import re
from dataclasses import dataclass

BANK_KOPRA = "1-1120"
BANK_LIVIN = "1-1130"  # Livin Amita Ratih
BANK_JAGO = "1-1140"
BANK_BCA_XPRESI = "1-1150"
BANK_LIVIN_LASTIKO = "1-1160"


# ─────────────────────────────────────────────────────────────────────────
# Operational bank-account configuration
# ─────────────────────────────────────────────────────────────────────────
# TODO(pipeline step): move this registry into Orion Settings so finance can
# add/remove operational accounts without a code change. Kept as a module
# constant for now to stay a verbatim port.
#
# Single source of truth for PT POI's company bank accounts. Inter-account
# transfers between any pair auto-route to the corresponding GL bank line
# (no Pinjaman / Modal / Suspense detour).
#
# When parsing bank statement of `gl_code`, an inflow/outflow whose remark
# contains another operational account's `account_no` is an inter-account
# transfer routed to that other account's GL.
#
# Note: BCA 7310116801 (Lastiko's BCA "Owner") is a registered-as-COMPANY
# account but NOT mapped to a GL bank line — by business decision its
# inflows are owner-loan (2-1510), not inter-account transfers. So it does
# NOT belong here. (See the explicit 7310116801 rule in _RULES.)
#
# Note: Bank Jago uses virtual "pockets", each with its own account number.
# ONLY pocket 103597714740 is PT POI's company account (the one whose
# statement gets imported). Other Jago pockets are Lastiko's personal funds →
# owner loan (2-1510), not inter-account. Hence only this pocket number is
# listed here; a Jago reference without this exact number is treated as
# Lastiko's personal money (via the LASTIKO name rule).
OPERATIONAL_ACCOUNTS: list[tuple[str, str, str]] = [
	# (account_no, gl_code, friendly_name)
	("1660007677776", BANK_KOPRA,         "Mandiri Kopra (PT POI)"),
	("1660007212202", BANK_LIVIN,         "Mandiri Livin (Amita Ratih)"),
	("1640005991353", BANK_LIVIN_LASTIKO, "Mandiri Livin (Lastiko)"),
	("103597714740",  BANK_JAGO,          "Bank Jago (pocket PT POI 103597714740)"),
	("5015288954",    BANK_BCA_XPRESI,    "BCA Xpresi (Amita Ratih)"),
]


def is_operational_account(account_no: str) -> bool:
	"""True when we are importing one of PT POI's own operational statements."""
	return any(acc == account_no for acc, _gl, _n in OPERATIONAL_ACCOUNTS)


def counterparty_operational_account(current_account_no: str, remark: str) -> str | None:
	"""The other operational account this line transfers to/from, or None.

	A transfer between two operational accounts appears on BOTH statements and
	must be booked once, not twice. We can only recognise the counterparty when
	its full account number is present in the remark — the reliable signal both
	statements can share (bank-name-only remarks are best-effort, handled at a
	higher layer). Returns the counterparty `account_no` when this is such an
	internal transfer, else None.
	"""
	if not is_operational_account(current_account_no):
		return None
	for acc, _gl, _name in OPERATIONAL_ACCOUNTS:
		if acc == current_account_no:
			continue
		if re.search(rf"\b{re.escape(acc)}\b", remark):
			return acc
	return None


def internal_transfer_hash(
	current_account_no: str, counterparty_account_no: str, date_key: str, amount: float
) -> str:
	"""Symmetric dedup hash for an internal transfer.

	Both statements of one transfer produce the IDENTICAL hash (the account pair
	is sorted, so direction and which-side-is-importing don't matter), so the
	second import collides on the txn_hash UNIQUE constraint and is dropped —
	the same mechanism that already dedups same-account re-imports.
	"""
	lo, hi = sorted([current_account_no, counterparty_account_no])
	return f"XFER|{lo}|{hi}|{date_key[:10]}|{amount:.2f}"


def _build_inter_account_rules(current_gl: str) -> list["MappingRule"]:
	"""Generate inter-account-transfer rules for the bank whose statement
	is being parsed (`current_gl`). Skips itself."""
	rules: list[MappingRule] = []
	for acc_no, gl_code, name in OPERATIONAL_ACCOUNTS:
		if gl_code == current_gl:
			continue
		rules.append(
			MappingRule(
				pattern=re.compile(rf"\b{re.escape(acc_no)}\b"),
				other_code=gl_code,
				label=f"Transfer antar rekening operasional → {name}",
				confidence="high",
			)
		)
	return rules


@dataclass
class MappingRule:
	# `pattern` may be None for an amount-only rule (e.g. a fixed bank fee that
	# has no reliable keyword); such a rule MUST set `amount`.
	pattern: re.Pattern | None
	other_code: str
	label: str
	confidence: str  # "high" | "medium" | "low"
	inflow_only: bool = False   # if True, rule only fires on bank-credit (money-in) lines
	outflow_only: bool = False  # if True, rule only fires on bank-debit (money-out) lines
	amount: float | None = None  # nominal: if set, the bank-movement amount must equal this (±0.01)


@dataclass
class SuggestedAccounts:
	debit_code: str | None
	credit_code: str | None
	label: str
	confidence: str  # "high" | "medium" | "low" | "none"


# Generic rules, evaluated in order — first match wins.
_RULES: list[MappingRule] = [
	# Universal bank fee by nominal: a Rp 2.500 money-out line is the standard
	# transfer/admin fee on every bank (Kopra/Livin/BCA/Lastiko), regardless of
	# description. Amount-only rule (no keyword needed).
	MappingRule(None, "6-1800", "Beban Administrasi/Transfer Bank (Rp 2.500)", "high", outflow_only=True, amount=2500),
	MappingRule(re.compile(r"biaya adm", re.I), "6-1800", "Beban Administrasi Bank", "high"),
	MappingRule(re.compile(r"biaya transfer|biaya txn|bif biaya", re.I), "6-1800", "Beban Transfer Bank", "high"),
	# Tax rules must come BEFORE the generic Bunga/Interest rule, otherwise
	# "Pajak Bunga" / "Tax on Interest" misfire to 7-1100 Pendapatan Bunga.
	MappingRule(
		re.compile(r"tax on interest|pajak bunga|pajak rekening|pajak tabungan", re.I),
		"6-1900",
		"Beban Pajak (rekening/bunga)",
		"high",
	),
	# Interest INCOME only — inflow-guarded so an outflow that mentions "bunga"
	# (e.g. "bunga pinjaman" = interest EXPENSE) is NOT mis-posted as income to
	# 7-1100. Such outflows fall through to manual rather than booking the wrong
	# side; add a dedicated interest-expense rule if those become common.
	MappingRule(re.compile(r"\bbunga\b|\binterest\b", re.I), "7-1100", "Pendapatan Bunga", "high", inflow_only=True),
	# NOTE: DP / Termin / Pelunasan keyword rule lives in
	# `_HIGH_PRIORITY_RULES` so it beats bank-specific inter-account
	# rules. See `suggest_accounts` for the evaluation order.
	# Owner-funded inflows route to per-owner sub-accounts (2-1510 Lastiko,
	# 2-1520 Amita) so the GL gives a per-person running balance of "how
	# much the company still owes". Sender-name match is more reliable
	# than channel keywords because owners have multiple personal/parking
	# accounts (BCA, Jago, UOB, ...). DP-keyword rule above already takes
	# precedence when a transfer is clearly a relayed client payment.
	# Lastiko's personal BCA 7310116801 is NOT a company account — funds
	# to/from it are owner loans (2-1510), not inter-account transfers.
	# Matched by account number so it works even when "LASTIKO" is absent.
	MappingRule(
		re.compile(r"\b7310116801\b"),
		"2-1510",
		"Pinjaman dari Lastiko Harmantyo (BCA pribadi 7310116801)",
		"high",
	),
	MappingRule(
		re.compile(r"\bLASTIKO\b", re.I),
		"2-1510",
		"Pinjaman dari Lastiko Harmantyo (operational funding)",
		"high",
	),
	MappingRule(
		re.compile(r"\bAMITA\s+RATIH\b", re.I),
		"2-1520",
		"Pinjaman dari Amita Ratih Purnamasari (operational funding)",
		"high",
	),
	# Channel-keyword fallbacks — for legacy references that don't include
	# the sender's full name (e.g. "PRM-MYBCA", "JAGBIDJA"). Routed to the
	# catch-all 2-1500 since we can't tell which owner without a name.
	MappingRule(
		re.compile(r"mybca|prm-mybca|prma cr transf", re.I),
		"2-1500",
		"Pinjaman dari Pemilik (transfer pribadi — owner unknown)",
		"medium",
	),
	MappingRule(
		re.compile(r"jagbidja", re.I),
		"2-1500",
		"Pinjaman dari Pemilik (transfer Jago — owner unknown)",
		"medium",
	),
	MappingRule(
		re.compile(r"YUSUP\s*SUBAGIO|1660007212269", re.I),
		"5-1100",
		"Biaya Material - Ratunda (transfer ke Yusup)",
		"medium",
	),
	MappingRule(
		re.compile(r"BCA\s*AMITA|5015288954", re.I),
		"3-1400",
		"Prive / Pengambilan Pribadi",
		"high",
	),
	MappingRule(
		re.compile(r"PENCIPTA ORGANIK IMAJI", re.I),
		BANK_KOPRA,
		"Transfer antar rekening perusahaan (Kopra)",
		"high",
	),
	MappingRule(re.compile(r"amanah legal", re.I), "2-1600", "Dana Titipan", "high"),
]

_BCA_XPRESI_RULES: list[MappingRule] = [
	# BCA online/instant-transfer fee by nominal — Rp 10.000 money-out
	# (recurring on BCA Xpresi, distinct from the universal Rp 2.500 fee).
	MappingRule(None, "6-1800", "Beban Transfer Bank - BCA (Rp 10.000)", "high", outflow_only=True, amount=10000),
	# PENCIPTA ORGANIK keyword for the company-name leg of Kopra inflows
	# (account 1660007677776 already covered by inter-account rule).
	MappingRule(re.compile(r"PENCIPTA ORGANIK", re.I), BANK_KOPRA, "Transfer ke Mandiri Kopra", "high"),
	MappingRule(
		re.compile(r"kartu kredit|flazz|transaksi debit|qr\s*\d|dana\b", re.I),
		"3-1400",
		"Prive / Pengeluaran Pribadi",
		"medium",
	),
]

_JAGO_RULES: list[MappingRule] = [
	MappingRule(re.compile(r"PENCIPTA ORGANIK", re.I), BANK_KOPRA, "Transfer ke/dari Mandiri Kopra", "high"),

	# Failed card-payment fee (Visa "Insufficient Funds Fee") → bank admin cost.
	# MUST precede the Anthropic/Claude rule because the fee text also contains
	# "ANTHROPIC". (Rp 5.000 each on this pocket.)
	MappingRule(re.compile(r"insufficient funds fee", re.I), "6-1800", "Beban Administrasi Bank (failed payment fee)", "high"),

	# Anthropic / Claude software subscription — a SUCCESSFUL card charge
	# (money out). outflow_only so the incoming "Claude mami / Claude Subs"
	# top-ups from Lastiko stay owner-loan, not expense. Precedes the generic
	# POS→prive rule so it isn't mistaken for personal spending.
	MappingRule(re.compile(r"\bANTHROPIC\b|\bCLAUDE\b", re.I), "6-2300", "Beban Software & Langganan (Anthropic/Claude)", "high", outflow_only=True),

	# "Movement between Pockets dari …" — money pulled from Lastiko's other
	# (personal) Jago pockets into the PT POI pocket to add capital → owner
	# loan from Lastiko.
	MappingRule(re.compile(r"Movement between Pockets", re.I), "2-1510", "Pinjaman dari Lastiko Harmantyo (movement antar pocket)", "high"),

	MappingRule(re.compile(r"DBS|7802739214", re.I), "2-1700", "Cicilan Pinjaman DBS", "high"),
	MappingRule(
		re.compile(r"POS Transaction|Top Up Wallet|SHOPEE|RUPARUPA|IKEA|STEAM", re.I),
		"3-1400",
		"Prive / Pengeluaran Pribadi",
		"medium",
	),
	MappingRule(re.compile(r"Tax on Interest", re.I), "6-1900", "Beban Pajak Bunga", "high"),
]

# Mandiri Livin (1-1130) — inter-account routing plus its recurring instant-
# transfer fee (Rp 6.000 money-out), distinct from the universal Rp 2.500 fee.
_LIVIN_RULES: list[MappingRule] = [
	MappingRule(None, "6-1800", "Beban Transfer Bank - Livin (Rp 6.000)", "high", outflow_only=True, amount=6000),
]

# Mandiri Kopra (1-1120) — payroll references follow `SS<initials>[-]YYMMDD[-]NN`.
# The `SS` prefix marks "Salary Settlement"; the alphabetic block after it
# encodes the employee initials. Two sub-patterns matter:
#   • SSI<X>… (X ≠ R) → magang/internship payroll. Examples: SSIMH, SSIMA, SSIAFR.
#   • SSIR…           → Irawan (full-time, exception to the SSI=magang rule).
#   • SS<X>… (X ≠ I)  → regular full-time payroll. Examples: SSDI, SSNI, SSMC,
#                       SSYF, SSRA.
# Numeric tail is informational only; we don't anchor on it (uploaders sometimes
# omit dashes — `SSIMA260328-03` vs `SSDI-260328-01`). Internship lines still
# post to 6-1100 today but the suggestion label flags them so finance can split
# them out into a dedicated magang account later if desired.
#
# Worker wage advances follow `<R|P>-CAU<n>-KN<nn>-26<roman>` where the leading
# letter is the business line: R=Ratunda, P=Poiesis. Recipient is currently
# Yusup most of the time but the rule does NOT anchor on the recipient name —
# the reference prefix is the source of truth. This deliberately overrides the
# generic Yusup→5-1100 rule for CAU references; non-CAU Yusup transfers
# (R-PO/R-CAM/R-MAT) still fall through to the material rule.
_KOPRA_RULES: list[MappingRule] = [
	# NOTE: inter-account transfer rules (5015288954, 103597714740,
	# 1660007212202) are auto-generated from OPERATIONAL_ACCOUNTS by
	# `_build_inter_account_rules` and prepended at evaluation time —
	# see suggest_accounts(). Don't add them here.
	#
	# IMPORTANT: the bank's statement appends the literal words "Transfer Fee"
	# to the END of MANY ordinary in-house transfer descriptions (e.g.
	# "MCM InhouseTrf KE YUSUP - R-CAU07-KN04-26IV-LV Transfer Fee"). So
	# "transfer fee" text is NOT a reliable fee signal here. The real fee is
	# identified by the fee transaction code 99102 (and/or the fixed nominal
	# caught by the amount rules in _RULES). The 99102 keyword rule therefore
	# lives at the BOTTOM of this list, below all doc-reference (CAxx/PO/PON/
	# SPK/BV) rules, so project transfers always win.

	# Payroll: Irawan (full-time, special-case before the magang rule).
	MappingRule(
		re.compile(r"\bSSIR(?![A-Z])", re.I),
		"6-1100",
		"Beban Gaji & Tunjangan (Irawan, full-time)",
		"high",
	),
	# Payroll: magang/internship — SSI followed by another letter (not R).
	MappingRule(
		re.compile(r"\bSSI[A-QS-Z][A-Z]*", re.I),
		"6-1100",
		"Beban Gaji & Tunjangan (Magang/Internship)",
		"high",
	),
	# Payroll: full-time regular — SS followed by initials that don't start with I.
	MappingRule(
		re.compile(r"\bSS[A-HJ-Z][A-Z]+", re.I),
		"6-1100",
		"Beban Gaji & Tunjangan",
		"high",
	),

	# ─── Project document-reference rules (business-line prefix) ─────────
	# Match dashed and non-dashed forms ("R-CAU" / "RCAU"; bare "CAU"/"CAM"
	# default to Ratunda). For each doc code the Poiesis (P-) rule precedes
	# the Ratunda/bare rule so a "P-…" reference never falls through to
	# Ratunda.
	#
	# The code is matched as a standalone TOKEN via `(?:\d|\b)`: it may be
	# followed by its running number ("CAU07", "R-CAU07-KN04") OR end at a
	# word boundary ("CAU LVNT LCTR ANPJ" — code then space-separated project
	# codes, no running number). This still rejects prose like "CAUSE"/
	# "BECAUSE" (no boundary between U and the next letter, and no digit).
	# Only the distinctive doc codes (CAO/CAU/CAM/PON) get this relaxation;
	# short/ambiguous codes (PO/SPK/BV) keep requiring a digit below.

	# CAO — Operasional & Kas Proyek (bare CAO → Ratunda).
	MappingRule(re.compile(r"\bP-?CAO(?:\d|\b)", re.I), "5-2500", "Biaya Operasional dan Kas Proyek - Poiesis", "high"),
	MappingRule(re.compile(r"\bR?-?CAO(?:\d|\b)", re.I), "5-1600", "Biaya Operasional dan Kas Proyek - Ratunda", "high"),

	# CAU — Upah Tukang (bare CAU → Ratunda).
	MappingRule(re.compile(r"\bP-?CAU(?:\d|\b)", re.I), "5-2900", "Upah Tukang / Pekerja - Poiesis", "high"),
	MappingRule(re.compile(r"\bR?-?CAU(?:\d|\b)", re.I), "5-1200", "Upah Tukang / Pekerja - Ratunda", "high"),

	# ─── Utilities / internet providers ──────────────────────────────────
	# Must come BEFORE the BV rule so a BV-prefixed reference that ALSO
	# mentions a provider goes to 6-1300 Utilitas, not 6-1500 ATK.
	MappingRule(
		re.compile(r"\bbiznet\b|\bindihome\b|\bfirstmedia\b|\bmyrepublic\b", re.I),
		"6-1300",
		"Beban Utilitas (Internet)",
		"high",
	),

	# ─── Office supplies & petty equipment — voucher refs ───────────────
	# `BV-2026-NN`, `BVI-2026-NN`, `POI-BVI18-2026`, `R-BVI20-2604`.
	# User rule: any reference containing BV (uppercase) → 6-1500 ATK.
	# Case-sensitive to avoid false hits inside random unrelated words.
	MappingRule(
		re.compile(r"\bBVI?-?\d"),
		"6-1500",
		"Beban ATK & Perlengkapan",
		"high",
	),

	# ─── Material — direct purchase to vendor (`<R|P>-PO<n>...`) ────────
	# PO = Purchase Order. Buyer is PT POI (us) paying the toko/vendor
	# directly, no mandor/tukang in the middle.
	MappingRule(
		re.compile(r"\bP-PO\d+", re.I),
		"5-2800",
		"Biaya Material - Poiesis (PO langsung ke vendor)",
		"high",
	),
	MappingRule(
		re.compile(r"\bR-PO\d+", re.I),
		"5-1100",
		"Biaya Material - Ratunda (PO langsung ke vendor)",
		"high",
	),

	# ─── Material — site-advance to mandor/tukang (`<P-?>CAM<n>...`) ────
	# CAM = Cash Advance Material. Money goes to mandor/tukang/kenek
	# (Yusup, Indra, etc.) who then buy material on the ground. Covers
	# R-CAM/RCAM, NARA-CAM, plain CAM (Ratunda default), P-CAM/PCAM.
	MappingRule(re.compile(r"\bP-?CAM(?:\d|\b)", re.I), "5-2800", "Biaya Material - Poiesis (advance ke mandor/tukang)", "high"),
	MappingRule(re.compile(r"\bR?-?CAM(?:\d|\b)", re.I), "5-1100", "Biaya Material - Ratunda (advance ke mandor/tukang)", "high"),

	# ─── PON — material bought via online PO (bare PON → Ratunda) ────────
	MappingRule(re.compile(r"\bP-?PON(?:\d|\b)", re.I), "5-2800", "Biaya Material - Poiesis (PO Online)", "high"),
	MappingRule(re.compile(r"\bR?-?PON(?:\d|\b)", re.I), "5-1100", "Biaya Material - Ratunda (PO Online)", "high"),

	# ─── SPK — subcontractor work order ─────────────────────────────────
	# SPK refs embed a vendor code right after "SPK" (no separating digit):
	# "R-SPKSISFIN-26IV-LVN", "R-SPKAQUI02-KN03-26I". So when an explicit
	# R-/P- business-line prefix is present we match SPK regardless of what
	# follows. A bare "SPK" still needs a trailing digit to avoid matching
	# the word "SPK" in prose.
	MappingRule(re.compile(r"\bP-?SPK", re.I), "5-2200", "Biaya Subkontraktor Desain - Poiesis", "high"),
	MappingRule(re.compile(r"\bR-?SPK", re.I), "5-1300", "Biaya Subkontraktor - Ratunda", "high"),
	MappingRule(re.compile(r"\bSPK\d", re.I), "5-1300", "Biaya Subkontraktor - Ratunda", "high"),

	# ─── Bank transfer/admin fee — LAST so doc references always win ─────
	# Matched by the fee transaction code 99102 only (NOT the generic
	# "Transfer Fee" suffix the bank appends to ordinary transfers). Fixed
	# nominal fees (Rp 2.500/6.000/10.000) are also caught by the amount
	# rules in _RULES as a backstop.
	MappingRule(re.compile(r"\b99102\b"), "6-1800", "Beban Administrasi Transfer Bank", "high"),
]

_BANK_SPECIFIC_RULES: dict[str, list[MappingRule]] = {
	BANK_BCA_XPRESI: _BCA_XPRESI_RULES,
	BANK_JAGO: _JAGO_RULES,
	BANK_KOPRA: _KOPRA_RULES,
	BANK_LIVIN: _LIVIN_RULES,
}


def _apply_rule(
	rule: MappingRule,
	remark: str,
	is_bank_credit: bool,
	is_bank_debit: bool,
	bank_account_code: str,
	amount: float = 0.0,
) -> SuggestedAccounts | None:
	if rule.inflow_only and not is_bank_credit:
		return None
	if rule.outflow_only and not is_bank_debit:
		return None
	if rule.amount is not None and abs(amount - rule.amount) > 0.01:
		return None
	if rule.pattern is not None and not rule.pattern.search(remark):
		return None
	# Safety: a rule with neither a pattern nor an amount would match anything.
	if rule.pattern is None and rule.amount is None:
		return None
	other_code = rule.other_code
	# Special case: inter-company transfer — Kopra row in Kopra rules → Livin
	if rule.other_code == BANK_KOPRA and bank_account_code == BANK_KOPRA:
		other_code = BANK_LIVIN

	if is_bank_credit:
		return SuggestedAccounts(
			debit_code=bank_account_code,
			credit_code=other_code,
			label=rule.label,
			confidence=rule.confidence,
		)
	if is_bank_debit:
		return SuggestedAccounts(
			debit_code=other_code,
			credit_code=bank_account_code,
			label=rule.label,
			confidence=rule.confidence,
		)
	return None


def _match_client_name(remark: str, client_names: list[str]) -> str | None:
	"""Return the longest client name found as a whole-word match in `remark`.

	Sorting by length descending avoids partial-name collisions (e.g. a client
	"Andre" wrongly matching "Andreas Sanders" if "Andreas" is also a client).
	"""
	upper = remark.upper()
	for name in sorted(client_names, key=len, reverse=True):
		n = (name or "").strip()
		if not n or len(n) < 3:  # skip pathological short names like "Mi", "Nia"
			continue
		# Whole-word, case-insensitive
		if re.search(rf"\b{re.escape(n)}\b", upper, re.I):
			return n
	return None


def default_client_names_provider() -> list[str]:
	"""Frappe-based client-name lookup (replaces the source's `clients` table).

	`frappe` is imported inside the function so this module stays importable
	(and unit-testable) without a Frappe site. When frappe is unavailable the
	fallback simply has no names to match, mirroring the source behaviour when
	the caller passed no `client_names`.
	"""
	try:
		import frappe
	except ImportError:
		return []
	if not getattr(frappe, "db", None):
		return []
	return frappe.get_all("Customer", pluck="customer_name")


_HIGH_PRIORITY_RULES: list[MappingRule] = [
	# Client-payment keywords — must beat ALL bank-specific rules
	# including inter-account routing. Per business spec: a transfer
	# description with any of these keywords is a client payment,
	# regardless of who/where it came from. Inflow-only so an
	# outbound "termin to vendor" or "final payment to supplier"
	# doesn't misfire to 2-1210.
	#
	# Coverage groups:
	#   - DP / Down Payment / Termin / Pelunasan / Lunas / Selesai
	#     (down-payment, instalment, settlement vocab)
	#   - FIN / FINAL / FINISH(ED) / FNL / COMPLETE(D)
	#     (final-payment shorthand seen in owner-relay descriptions
	#     like "FIN SUNRISE 10%", "Final ozone design & furn")
	MappingRule(
		re.compile(
			r"\bDP\s*\d*\b"
			r"|\bdown\s*payment\b"
			r"|\btermin\s*\d*\b"
			r"|\bpelunasan\b"
			r"|\blunas\b"
			r"|\bselesai\b"
			r"|\bFIN\b"
			r"|\bFNL\b"
			r"|\bfinal\b"
			r"|\bfinish(ed)?\b"
			r"|\bcomplete(d)?\b",
			re.I,
		),
		"2-1210",
		"DP / Pelunasan Klien - Ratunda (default; verify business line)",
		# Medium, not high: the credit line is a blind Ratunda default and the
		# business line (Ratunda vs Poiesis 2-1220) still needs operator review,
		# so this should surface for confirmation rather than auto-accept.
		"medium",
		inflow_only=True,
	),
]


def suggest_accounts(
	remark: str,
	debit: float,
	credit: float,
	bank_account_code: str = BANK_KOPRA,
	client_names: list[str] | None = None,
	client_names_provider=None,
) -> SuggestedAccounts:
	"""Suggest a CoA account for a single bank-statement transaction.

	Resolution order:
	  1. High-priority generic rules (`_HIGH_PRIORITY_RULES`) — run before
	     anything bank-specific so e.g. DP-keyword always wins over an
	     inter-account-transfer rule.
	  2. Bank-specific regex rules (`_BANK_SPECIFIC_RULES[bank_account_code]`)
	  3. Generic regex rules (`_RULES`)
	  4. (inflows only) known-client-name fallback — match `client_names`
	     against `remark` and route to 2-1210 if any client name appears as a
	     whole word. When `client_names` is None, names are fetched lazily via
	     `client_names_provider` (default: Customer names from frappe).
	  5. Empty fallback → "Perlu dikategorikan manual".
	"""
	is_bank_credit = credit > 0
	is_bank_debit = debit > 0
	# The bank-movement amount, used by amount-aware (nominal) rules.
	amount = debit if is_bank_debit else credit

	for rule in _HIGH_PRIORITY_RULES:
		r = _apply_rule(rule, remark, is_bank_credit, is_bank_debit, bank_account_code, amount)
		if r:
			return r

	# Inter-account transfer rules — generated from OPERATIONAL_ACCOUNTS so
	# adding/removing a company bank account is one-line.
	for rule in _build_inter_account_rules(bank_account_code):
		r = _apply_rule(rule, remark, is_bank_credit, is_bank_debit, bank_account_code, amount)
		if r:
			return r

	specific = _BANK_SPECIFIC_RULES.get(bank_account_code)
	if specific:
		for rule in specific:
			r = _apply_rule(rule, remark, is_bank_credit, is_bank_debit, bank_account_code, amount)
			if r:
				return r
	for rule in _RULES:
		r = _apply_rule(rule, remark, is_bank_credit, is_bank_debit, bank_account_code, amount)
		if r:
			return r

	# Inflow-only client-name fallback. Default business line is Ratunda
	# (2-1210); operator manually flips to 2-1220 (Poiesis) if needed.
	if is_bank_credit:
		if client_names is None:
			provider = client_names_provider or default_client_names_provider
			client_names = provider()
		if client_names:
			matched = _match_client_name(remark, client_names)
			if matched:
				return SuggestedAccounts(
					debit_code=bank_account_code,
					credit_code="2-1210",
					label=f"DP / Pelunasan Klien - Ratunda (terdeteksi nama: {matched}; default Ratunda — verify business line)",
					confidence="medium",
				)

	return SuggestedAccounts(
		debit_code=bank_account_code if is_bank_credit else None,
		credit_code=bank_account_code if is_bank_debit else None,
		label="Perlu dikategorikan manual",
		confidence="none",
	)
