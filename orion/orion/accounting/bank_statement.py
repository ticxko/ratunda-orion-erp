"""Bank-statement pipeline: PDF -> Claude parse -> auto-tag -> review inbox -> JE + Bank Transaction.

Port of `app/services/accounting/bank_statement_jobs.py` (job lifecycle, Claude
prompt/parse, 3-layer dedup) and the approve path of
`app/services/accounting/journal.py::create_batch` from Bellatrix Python,
re-homed on Frappe/ERPNext:

- jobs live in the "Orion Bank Statement Job" doctype (result stored as a JSON
  string with the SAME payload structure the source service stored, so
  feynman's review UI renders unchanged);
- background work runs through frappe.enqueue on the "long" queue;
- approval dual-writes: a submitted Journal Entry (source semantics) AND a
  submitted ERPNext Bank Transaction per row, reconciled against the JE via
  its payment_entries child table -> stock bank-rec reporting for free.

The Claude API key lives in site_config as `anthropic_api_key`. When it is
missing, jobs fail gracefully (status "failed" + error message).
"""

import base64
import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal

import frappe

from orion.accounting.account_mapper import (
	OPERATIONAL_ACCOUNTS,
	counterparty_operational_account,
	internal_transfer_hash,
	suggest_accounts,
)
from orion.accounting.pdf_password import maybe_decrypt
from orion.accounting.same_account_dedup import (
	BatchTxn,
	dup_key,
	find_same_account_duplicates,
)
from orion.accounting.txn_hash import compute_txn_hash, has_real_ref

JOB_DOCTYPE = "Orion Bank Statement Job"

# Source default (bellatrix-python pinned this model); Orion Settings
# `claude_model` overrides it without a code change.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 16384

# Indonesian bank account numbers are 9-15 digits. The Mandiri Livin AI parser
# sometimes pulls a counterparty's account number from the description into
# the `reference` field — but counterparty account numbers are NOT unique per
# transaction, so using them as the reference-portion of txn_hash causes
# false-positive dedup. Treat such "references" as no-ref so the hash falls
# back to bank|date|amount|description.
_BANK_ACCT_RE = re.compile(r"^\d{9,15}$")

# Combined bank payments that were split across projects live as sibling
# entries sharing one base legacy id with a `_s1`/`_s2`/… suffix. Summing a
# group back up recovers the original statement line so the same-account
# matcher still recognises it on a re-import.
_SPLIT_SUFFIX_RE = re.compile(r"_s\d+$")

_FENCE_OPEN = re.compile(r"^```(?:json)?\n?", re.M)
_FENCE_CLOSE = re.compile(r"\n?```$", re.M)

# Derived from the single source of truth in account_mapper.OPERATIONAL_ACCOUNTS.
ACCOUNT_TO_GL: dict = {acc_no: gl for acc_no, gl, _ in OPERATIONAL_ACCOUNTS}

BANK_FORMAT = {
	"mandiri kopra": "kopra",
	"kopra": "kopra",
	"mandiri livin": "livin",
	"livin": "livin",
	"bca": "bca-xpresi",
	"bca xpresi": "bca-xpresi",
	"jago": "jago",
	"bank jago": "jago",
}

BANK_STATEMENT_PROMPT = """Kamu adalah parser rekening koran bank. Tugas kamu adalah mengekstrak SEMUA transaksi dari rekening koran bank berikut secara akurat.

ATURAN PENTING:
1. Ekstrak SETIAP transaksi tanpa kecuali. Jangan skip atau ringkas. Termasuk transaksi di halaman pertama dan terakhir, transaksi kecil seperti BIF BIAYA TXN, BIAYA ADM, BUNGA, dan PAJAK BUNGA.
2. Semua angka harus berupa plain number tanpa formatting (misal: 17382794.78 bukan 17.382.794,78 atau 17,382,794.78)
3. debit = uang KELUAR dari rekening. credit = uang MASUK ke rekening. Salah satu HARUS 0.
4. Tanggal selalu format YYYY-MM-DD. Tanggal harus PERSIS sesuai kolom tanggal di rekening koran (jangan geser ±1 hari karena timezone atau settlement). Jika hanya DD/MM, ambil tahun dari periode statement.
5. Untuk balance: isi running balance setelah transaksi. Jika tidak tersedia untuk suatu transaksi, isi 0.
6. Untuk reference: ambil nomor referensi/ID transaksi internal bank (mis. BI Fast ID, Switching CR ref, kode unik per-transaksi). JANGAN gunakan nomor rekening counterparty (10-15 digit pure numeric yang muncul di description) sebagai reference — itu bukan ID transaksi. Jika tidak ada ID transaksi yang jelas dan unik per-baris, isi "-".
7. Untuk description: gabungkan tipe transaksi + keterangan + nama penerima/pengirim menjadi satu string yang informatif.
8. Untuk time: ambil jam transaksi jika tersedia (format HH:MM:SS atau HH:MM), jika tidak ada isi "00:00".
9. Untuk summary: WAJIB ekstrak kotak ringkasan rekening (Mutasi CR/DB total + count, Saldo Awal, Saldo Akhir) — biasanya di halaman terakhir.

Kembalikan JSON dengan struktur PERSIS berikut (tidak ada teks lain):
{
  "bankName": "nama bank singkat (misal: BCA, Mandiri Kopra, Mandiri Livin, Bank Jago, dll)",
  "accountNo": "nomor rekening",
  "accountName": "nama pemilik rekening",
  "period": "periode statement (misal: Desember 2025, atau 08 Feb 2026 - 15 Mar 2026)",
  "currency": "mata uang (misal: IDR)",
  "openingBalance": 0.00,
  "closingBalance": 0.00,
  "summaryBox": {
    "totalCredit": 0.00,
    "creditCount": 0,
    "totalDebit": 0.00,
    "debitCount": 0
  },
  "transactions": [
    {
      "date": "YYYY-MM-DD",
      "time": "HH:MM",
      "description": "deskripsi lengkap transaksi",
      "reference": "nomor referensi atau -",
      "debit": 0.00,
      "credit": 0.00,
      "balance": 0.00
    }
  ]
}

Kembalikan HANYA JSON yang valid, tanpa markdown, tanpa penjelasan tambahan."""


# ── job lifecycle ───────────────────────────────────────────────────────────


def create_job(file_name, content=None, file_url=None, source="manual"):
	"""Create a pending job, attach the (decrypted) PDF, enqueue the parse.

	`content` is raw PDF bytes (or a base64 string); alternatively pass
	`file_url` of an existing File. Decryption happens synchronously so a
	bad-password PDF fails the request, not the background job (matches the
	source router). Returns the inserted job doc.
	"""
	if content is None and file_url:
		content = frappe.get_doc("File", {"file_url": file_url}).get_content()
	if content is None:
		frappe.throw("No PDF content provided")
	if isinstance(content, str):
		content = base64.b64decode(content)

	content = maybe_decrypt(content, file_name or "statement.pdf")

	job = frappe.new_doc(JOB_DOCTYPE)
	job.status = "pending"
	job.file_name = file_name
	job.source = "auto" if source == "auto" else "manual"
	job.insert(ignore_permissions=True)

	frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name or ("%s.pdf" % job.name),
			"attached_to_doctype": JOB_DOCTYPE,
			"attached_to_name": job.name,
			"is_private": 1,
			"content": content,
		}
	).insert(ignore_permissions=True)

	frappe.db.commit()
	frappe.enqueue(
		"orion.accounting.bank_statement.process_job",
		queue="long",
		timeout=1800,
		job_name="orion-bank-statement-%s" % job.name,
		name=job.name,
	)
	return job


def process_job(name):
	"""Run parse + dedup pipeline. Updates the job to done/failed."""
	frappe.db.set_value(JOB_DOCTYPE, name, "status", "processing", update_modified=False)
	frappe.db.commit()
	try:
		pdf_bytes = _job_pdf_bytes(name)
		file_name = frappe.db.get_value(JOB_DOCTYPE, name, "file_name") or ""
		result_payload = _parse_statement(pdf_bytes, file_name)
		frappe.db.set_value(
			JOB_DOCTYPE,
			name,
			{
				"status": "done",
				"result": json.dumps(result_payload, ensure_ascii=False),
				"error": None,
				"bank_account_no": result_payload["info"]["accountNo"],
			},
		)
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		frappe.db.set_value(
			JOB_DOCTYPE, name, {"status": "failed", "error": str(e)[:1000]}
		)
		frappe.db.commit()
		frappe.log_error(
			title="Bank statement parse failed: %s" % name,
			message=frappe.get_traceback(),
		)


def _job_pdf_bytes(name):
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": JOB_DOCTYPE, "attached_to_name": name},
		order_by="creation asc",
		pluck="name",
	)
	if not files:
		raise RuntimeError("No PDF attached to job %s" % name)
	return frappe.get_doc("File", files[0]).get_content()


# ── Claude call + parse (ports process_job from the source service) ─────────


def _sanitize_reference(reference, remark):
	"""Drop references that look like a counterparty bank account number echoed
	from the description. Returns '-' (no real ref) in that case."""
	ref = (reference or "").strip()
	if not ref or ref == "-":
		return "-"
	if _BANK_ACCT_RE.fullmatch(ref) and ref in (remark or ""):
		return "-"
	return ref


def _resolve_format(bank_name):
	key = (bank_name or "").lower().strip()
	return BANK_FORMAT.get(key, key.replace(" ", "-"))


def _strip_fences(s):
	return _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", s.strip())).strip()


def _claude_model():
	model = frappe.get_cached_doc("Orion Settings").claude_model
	return model or DEFAULT_CLAUDE_MODEL


def _call_claude(pdf_bytes):
	api_key = frappe.conf.get("anthropic_api_key")
	if not api_key:
		raise RuntimeError(
			"anthropic_api_key is not configured in site_config — cannot parse bank statements"
		)
	import anthropic  # imported lazily so the app loads even before deps install

	client = anthropic.Anthropic(api_key=api_key)
	msg = client.messages.create(
		model=_claude_model(),
		max_tokens=CLAUDE_MAX_TOKENS,
		messages=[
			{
				"role": "user",
				"content": [
					{
						"type": "document",
						"source": {
							"type": "base64",
							"media_type": "application/pdf",
							"data": base64.b64encode(pdf_bytes).decode("ascii"),
						},
					},
					{"type": "text", "text": BANK_STATEMENT_PROMPT},
				],
			}
		],
	)
	first = msg.content[0] if msg.content else None
	return first.text.strip() if getattr(first, "type", None) == "text" else ""


def _parse_statement(pdf_bytes, file_name):
	"""PDF bytes -> result payload dict (same structure the source stored)."""
	raw = _call_claude(pdf_bytes)
	ai_result = json.loads(_strip_fences(raw))

	if not isinstance(ai_result.get("transactions"), list):
		raise RuntimeError("AI tidak mengembalikan daftar transaksi")

	account_no = str(ai_result.get("accountNo", "")).strip()
	bank_gl_code = ACCOUNT_TO_GL.get(account_no)
	if bank_gl_code is None:
		raise RuntimeError(
			"Unknown operational account '%s'. "
			"Register it in account_mapper.OPERATIONAL_ACCOUNTS before importing."
			% account_no
		)
	fmt = _resolve_format(str(ai_result.get("bankName", "")))

	summary_box = ai_result.get("summaryBox") or {}
	info = {
		"accountNo": account_no,
		"accountName": str(ai_result.get("accountName", "")),
		"period": str(ai_result.get("period", "")),
		"currency": str(ai_result.get("currency", "IDR")),
		"openingBalance": float(ai_result.get("openingBalance") or 0),
		"closingBalance": float(ai_result.get("closingBalance") or 0),
		"summaryBox": {
			"totalCredit": float(summary_box.get("totalCredit") or 0),
			"creditCount": int(summary_box.get("creditCount") or 0),
			"totalDebit": float(summary_box.get("totalDebit") or 0),
			"debitCount": int(summary_box.get("debitCount") or 0),
		},
	}

	accounts, code_to_account = _active_leaf_accounts()
	client_names = frappe.get_all(
		"Customer", filters={"disabled": 0}, pluck="customer_name"
	)

	# Pre-compute hashes for all transactions
	tx_list = []
	for idx, tx in enumerate(ai_result["transactions"]):
		debit = float(tx.get("debit") or 0)
		credit = float(tx.get("credit") or 0)
		remark = str(tx.get("description") or "")
		reference = _sanitize_reference(str(tx.get("reference") or "-"), remark)
		balance = float(tx.get("balance") or 0)
		amount = max(debit, credit)
		date_str = str(tx.get("date") or "")
		time_str = str(tx.get("time") or "00:00")
		try:
			full_iso = "%sT%s" % (
				date_str,
				time_str if len(time_str) > 5 else time_str + ":00",
			)
			iso_date = datetime.fromisoformat(full_iso).isoformat()
		except Exception:
			try:
				iso_date = datetime.fromisoformat(date_str).isoformat()
			except Exception:
				iso_date = date_str
		date_key = iso_date[:10]
		# A transfer between two operational accounts appears on BOTH
		# statements — hash it symmetrically so the second import dedups
		# against the first.
		counterparty = counterparty_operational_account(account_no, remark)
		if counterparty is not None:
			txn_hash = internal_transfer_hash(account_no, counterparty, date_key, amount)
			is_internal_xfer = True
		else:
			txn_hash = compute_txn_hash(account_no, date_key, reference, amount, remark)
			is_internal_xfer = False
		tx_list.append(
			{
				"idx": idx,
				"isoDate": iso_date,
				"dateKey": date_key,
				"remark": remark,
				"reference": reference,
				"debit": debit,
				"credit": credit,
				"balance": balance,
				"amount": amount,
				"txnHash": txn_hash,
				"isInternalXfer": is_internal_xfer,
			}
		)

	# Layer 1: dedup by txn_hash — against Journal Entries AND Bank Transactions
	all_hashes = [t["txnHash"] for t in tx_list]
	existing_hashes = _existing_hashes(all_hashes)

	# Layer 2: cross-account reference dedup (same ref + date + amount, different bank account)
	refs_to_check = list({t["reference"] for t in tx_list if has_real_ref(t["reference"])})
	if refs_to_check:
		existing_ref_keys = _existing_ref_keys(refs_to_check, exclude_account_no=account_no)
		for t in tx_list:
			if has_real_ref(t["reference"]):
				if _ref_key(t["reference"], t["dateKey"], t["amount"]) in existing_ref_keys:
					existing_hashes.add(t["txnHash"])

	# Intra-batch dedup
	seen_ref_keys = set()
	for t in tx_list:
		if has_real_ref(t["reference"]):
			key = _ref_key(t["reference"], t["dateKey"], t["amount"])
			if key in seen_ref_keys:
				existing_hashes.add(t["txnHash"])
			seen_ref_keys.add(key)

	# Layer 3: same-account dedup on date + amount + direction (count-aware)
	bank_account_row = code_to_account.get(bank_gl_code)
	same_account_dups = set()
	if bank_account_row:
		ledger_counts = _ledger_occurrences(
			account_no, bank_account_row.name, {t["dateKey"] for t in tx_list}
		)
		same_account_dups = find_same_account_duplicates(
			[
				BatchTxn(
					idx=t["idx"],
					date_key=t["dateKey"],
					amount=t["amount"],
					direction="OUT" if t["debit"] > 0 else "IN",
					already_duplicate=t["txnHash"] in existing_hashes,
				)
				for t in tx_list
			],
			ledger_counts,
		)

	transactions = []
	for t in tx_list:
		sug = suggest_accounts(
			t["remark"],
			t["debit"],
			t["credit"],
			bank_gl_code,
			client_names=client_names,
		)
		debit_acct = code_to_account.get(sug.debit_code) if sug.debit_code else None
		credit_acct = code_to_account.get(sug.credit_code) if sug.credit_code else None
		hash_dup = t["txnHash"] in existing_hashes
		is_dup = hash_dup or t["idx"] in same_account_dups
		dup_reason = None
		if is_dup:
			if not hash_dup:
				dup_reason = "same_account"
			elif t.get("isInternalXfer"):
				dup_reason = "internal_transfer"
			elif has_real_ref(t["reference"]):
				dup_reason = "ref"
			else:
				dup_reason = "hash"
		transactions.append(
			{
				"id": t["idx"],
				"date": t["isoDate"],
				"remark": t["remark"],
				"reference": t["reference"],
				"debit": t["debit"],
				"credit": t["credit"],
				"balance": t["balance"],
				"txnHash": t["txnHash"],
				"isDuplicate": is_dup,
				"duplicateReason": dup_reason,
				"suggestion": {
					"label": sug.label,
					"confidence": sug.confidence,
					"debitAccountId": _account_id(debit_acct),
					"creditAccountId": _account_id(credit_acct),
				},
			}
		)

	duplicate_count = sum(1 for t in transactions if t["isDuplicate"])

	# Reconciliation gate: compare extracted transactions to the bank's summary
	# box so silently dropped transactions surface BEFORE import.
	sum_credit = sum(float(t["credit"] or 0) for t in transactions)
	sum_debit = sum(float(t["debit"] or 0) for t in transactions)
	cnt_credit = sum(1 for t in transactions if float(t["credit"] or 0) > 0)
	cnt_debit = sum(1 for t in transactions if float(t["debit"] or 0) > 0)
	sb = info["summaryBox"]
	recon_issues = []
	if sb["totalCredit"] > 0 and abs(sum_credit - sb["totalCredit"]) > 0.01:
		recon_issues.append(
			"Sum of CREDIT transactions (%.2f) ≠ bank summary totalCredit (%.2f); selisih %.2f"
			% (sum_credit, sb["totalCredit"], sum_credit - sb["totalCredit"])
		)
	if sb["totalDebit"] > 0 and abs(sum_debit - sb["totalDebit"]) > 0.01:
		recon_issues.append(
			"Sum of DEBIT transactions (%.2f) ≠ bank summary totalDebit (%.2f); selisih %.2f"
			% (sum_debit, sb["totalDebit"], sum_debit - sb["totalDebit"])
		)
	if sb["creditCount"] > 0 and cnt_credit != sb["creditCount"]:
		recon_issues.append(
			"Credit txn count (%d) ≠ bank summary creditCount (%d)"
			% (cnt_credit, sb["creditCount"])
		)
	if sb["debitCount"] > 0 and cnt_debit != sb["debitCount"]:
		recon_issues.append(
			"Debit txn count (%d) ≠ bank summary debitCount (%d)"
			% (cnt_debit, sb["debitCount"])
		)
	if info["openingBalance"] and info["closingBalance"]:
		implied_close = info["openingBalance"] + sum_credit - sum_debit
		if abs(implied_close - info["closingBalance"]) > 0.01:
			recon_issues.append(
				"Opening + Σcredit − Σdebit = %.2f ≠ closingBalance %.2f; selisih %.2f"
				% (implied_close, info["closingBalance"], implied_close - info["closingBalance"])
			)

	return {
		"info": info,
		"transactions": transactions,
		"accounts": [
			{
				"id": _account_id(a),
				"code": a.account_number or "",
				"name": a.account_name,
				"type": _orion_type(a),
				"normalBalance": _normal_balance(a.root_type),
			}
			for a in accounts
		],
		"fileName": file_name,
		"format": fmt,
		"duplicateCount": duplicate_count,
		"reconciliation": {
			"extracted": {
				"sumCredit": sum_credit,
				"sumDebit": sum_debit,
				"creditCount": cnt_credit,
				"debitCount": cnt_debit,
			},
			"issues": recon_issues,
			"ok": not recon_issues,
		},
	}


# ── dedup lookups (ERPNext-backed) ──────────────────────────────────────────


def _existing_hashes(hashes):
	"""Hashes already booked on Journal Entries OR Bank Transactions."""
	if not hashes:
		return set()
	existing = set(
		frappe.get_all(
			"Journal Entry",
			filters={"orion_txn_hash": ("in", hashes), "docstatus": ("!=", 2)},
			pluck="orion_txn_hash",
		)
	)
	if frappe.db.has_column("Bank Transaction", "orion_txn_hash"):
		existing |= set(
			frappe.get_all(
				"Bank Transaction",
				filters={"orion_txn_hash": ("in", hashes), "docstatus": ("!=", 2)},
				pluck="orion_txn_hash",
			)
		)
	existing.discard(None)
	return existing


def _ref_key(reference, date_key, amount):
	amt = Decimal(str(amount))
	amt_str = str(int(amt)) if amt == amt.to_integral_value() else str(float(amt))
	return "%s|%s|%s" % (reference, date_key, amt_str)


def _existing_ref_keys(refs, exclude_account_no=None):
	"""'ref|date|amount' keys of already-posted BANK_IMPORT JEs with these refs."""
	filters = {
		"cheque_no": ("in", refs),
		"orion_source_type": "BANK_IMPORT",
		"docstatus": ("!=", 2),
	}
	if exclude_account_no:
		filters["orion_bank_account_no"] = ("!=", exclude_account_no)
	entries = frappe.get_all(
		"Journal Entry", filters=filters, fields=["name", "posting_date", "cheque_no"]
	)
	if not entries:
		return set()
	lines_by_parent = _je_lines_by_parent([e.name for e in entries])
	keys = set()
	for e in entries:
		amount = max(
			(
				max(Decimal(str(l.debit_in_account_currency or 0)), Decimal(str(l.credit_in_account_currency or 0)))
				for l in lines_by_parent.get(e.name, [])
			),
			default=Decimal("0"),
		)
		keys.add(_ref_key(e.cheque_no, str(e.posting_date)[:10], amount))
	return keys


def _je_lines_by_parent(names):
	lines_by_parent = {}
	for l in frappe.get_all(
		"Journal Entry Account",
		filters={"parenttype": "Journal Entry", "parent": ("in", names)},
		fields=["parent", "account", "debit_in_account_currency", "credit_in_account_currency"],
	):
		lines_by_parent.setdefault(l.parent, []).append(l)
	return lines_by_parent


def _ledger_occurrences(account_no, bank_gl_account, date_keys):
	"""Count BANK_IMPORT transactions already posted for this bank account,
	keyed by (date, amount, direction). Cancelled docs, reversal entries and
	reversed entries are excluded; `_sN` split siblings are re-joined by base
	legacy id so a re-import of the original line still matches."""
	counts = Counter()
	if not date_keys:
		return counts

	filters = {
		"orion_bank_account_no": account_no,
		"orion_source_type": "BANK_IMPORT",
		"posting_date": ("between", [min(date_keys), max(date_keys)]),
		"docstatus": ("!=", 2),
	}
	has_reversal = frappe.db.has_column("Journal Entry", "reversal_of")
	if has_reversal:
		filters["reversal_of"] = ("is", "not set")
	entries = frappe.get_all(
		"Journal Entry",
		filters=filters,
		fields=["name", "posting_date", "orion_legacy_id"],
	)
	if not entries:
		return counts

	if has_reversal:
		reversed_names = set(
			frappe.get_all(
				"Journal Entry",
				filters={"reversal_of": ("is", "set"), "docstatus": ("!=", 2)},
				pluck="reversal_of",
			)
		)
		entries = [e for e in entries if e.name not in reversed_names]
		if not entries:
			return counts

	lines_by_parent = _je_lines_by_parent([e.name for e in entries])

	# (base id, date, direction) -> summed amount; re-joins split siblings.
	groups = {}
	for e in entries:
		bank_line = next(
			(l for l in lines_by_parent.get(e.name, []) if l.account == bank_gl_account),
			None,
		)
		if bank_line is None:
			continue
		debit = Decimal(str(bank_line.debit_in_account_currency or 0))
		credit = Decimal(str(bank_line.credit_in_account_currency or 0))
		direction = "IN" if debit > 0 else "OUT"
		amount = debit if debit > 0 else credit
		if amount <= 0:
			continue
		date_key = str(e.posting_date)[:10]
		if date_key not in date_keys:
			continue
		base_id = _SPLIT_SUFFIX_RE.sub("", e.orion_legacy_id or e.name)
		key = (base_id, date_key, direction)
		groups[key] = groups.get(key, Decimal("0")) + amount

	for (_base_id, date_key, direction), amount in groups.items():
		counts[dup_key(date_key, amount, direction)] += 1

	return counts


# ── account helpers ─────────────────────────────────────────────────────────

# Reverse of migrate/coa.py ROOT_TYPE — same mapping compat/accounting.py uses,
# duplicated here so accounting does not import the compat gateway package.
TYPE_BY_PREFIX = {
	"4": "REVENUE",
	"5": "COGS",
	"6": "EXPENSE",
	"7": "OTHER_INCOME",
	"8": "OTHER_EXPENSE",
}
TYPE_BY_ROOT = {
	"Asset": "ASSET",
	"Liability": "LIABILITY",
	"Equity": "EQUITY",
	"Income": "REVENUE",
	"Expense": "EXPENSE",
}


def _orion_type(row):
	prefix = (row.account_number or "")[:1]
	if prefix in TYPE_BY_PREFIX:
		return TYPE_BY_PREFIX[prefix]
	return TYPE_BY_ROOT.get(row.root_type, "ASSET")


def _normal_balance(root_type):
	return "DEBIT" if root_type in ("Asset", "Expense") else "CREDIT"


def _company():
	company = frappe.get_cached_doc("Orion Settings").company
	if not company:
		frappe.throw("Orion Settings has no company")
	return company


def _account_id(row):
	if not row:
		return None
	return row.orion_legacy_id or row.name


def _active_leaf_accounts():
	"""(sorted active leaf account rows, code -> row map)."""
	rows = frappe.get_all(
		"Account",
		filters={"company": _company(), "disabled": 0, "is_group": 0},
		fields=[
			"name", "account_name", "account_number", "root_type",
			"orion_business_line", "orion_legacy_id",
		],
		order_by="account_number asc, name asc",
	)
	return rows, {r.account_number: r for r in rows if r.account_number}


def _resolve_account_name(ident):
	"""accountId (legacy id) or Account name -> Account name."""
	name = frappe.db.get_value("Account", {"orion_legacy_id": ident})
	if not name and frappe.db.exists("Account", ident):
		name = ident
	if not name:
		frappe.throw("Unknown account id '%s'" % ident)
	return name


def _cost_center_for(account_name, settings):
	line = frappe.db.get_value("Account", account_name, "orion_business_line")
	if line == "RATUNDA_RENOVASI":
		return settings.ratunda_cost_center
	if line == "POIESIS_STUDIO":
		return settings.poiesis_cost_center
	return settings.shared_cost_center


# ── approve: JE + Bank Transaction dual-write ───────────────────────────────


def approve(name, entries):
	"""Create submitted Journal Entries + reconciled Bank Transactions from the
	accepted review rows. Idempotent by txn_hash — rows whose hash is already
	on a JE or Bank Transaction are skipped (same semantics as the source
	`JournalEntryService.create_batch`). Returns
	{"created": n, "skipped": m, "skippedList": [...], "journalEntries": [...]}.

	Each entry (feynman shape, camelCase):
	{date, description, reference?, sourceType, sourceFile, bankAccount,
	 txnHash?, lines: [{accountId, debit, credit}, ...]}
	"""
	job = frappe.get_doc(JOB_DOCTYPE, name)
	settings = frappe.get_cached_doc("Orion Settings")

	for entry in entries:
		if not entry.get("date") or not entry.get("description") or not entry.get("lines"):
			frappe.throw("Each entry needs date, description, and at least one line")
		total_debit = sum(Decimal(str(l.get("debit") or 0)) for l in entry["lines"])
		total_credit = sum(Decimal(str(l.get("credit") or 0)) for l in entry["lines"])
		if total_debit != total_credit:
			frappe.throw("Unbalanced entry: %s" % entry.get("description"))

	# Hash per entry (BANK_IMPORT only), with within-batch collision suffixing
	# — real-world BCA batch references are shared across beneficiaries.
	hash_map = {}
	for idx, entry in enumerate(entries):
		if entry.get("sourceType") != "BANK_IMPORT" or not entry.get("bankAccount"):
			continue
		if entry.get("txnHash"):
			hash_map[idx] = entry["txnHash"]
		else:
			hash_map[idx] = compute_txn_hash(
				entry["bankAccount"],
				str(entry["date"])[:10],
				entry.get("reference") or "-",
				float(_entry_max_amount(entry["lines"])),
				entry["description"],
			)
	hash_counts = Counter(hash_map.values())
	if any(c > 1 for c in hash_counts.values()):
		seen = {}
		for idx in sorted(hash_map.keys()):
			h = hash_map[idx]
			if hash_counts[h] > 1:
				seen[h] = seen.get(h, 0) + 1
				if seen[h] > 1:
					hash_map[idx] = "%s|%s" % (h, seen[h])

	skipped_hashes = _existing_hashes(list(hash_map.values()))

	# Cross-account reference dedup (all accounts, like the source create_batch)
	refs_to_check = list(
		{
			entry["reference"]
			for entry in entries
			if entry.get("sourceType") == "BANK_IMPORT" and has_real_ref(entry.get("reference"))
		}
	)
	if refs_to_check:
		existing_ref_keys = _existing_ref_keys(refs_to_check)
		for idx, entry in enumerate(entries):
			if not has_real_ref(entry.get("reference")) or idx not in hash_map:
				continue
			key = _ref_key(
				entry["reference"], str(entry["date"])[:10], _entry_max_amount(entry["lines"])
			)
			if key in existing_ref_keys:
				skipped_hashes.add(hash_map[idx])

	created = []
	skipped_list = []
	for idx, entry in enumerate(entries):
		h = hash_map.get(idx)
		if h and h in skipped_hashes:
			skipped_list.append("%s — %s" % (str(entry["date"])[:10], entry["description"]))
			continue
		je = _create_journal_entry(entry, h, settings)
		if h and entry.get("bankAccount"):
			_create_bank_transaction(entry, h, je, job, settings)
		created.append(je.name)

	return {
		"created": len(created),
		"skipped": len(skipped_list),
		"skippedList": skipped_list,
		"journalEntries": created,
	}


def _entry_max_amount(lines):
	return max(
		(
			max(Decimal(str(l.get("debit") or 0)), Decimal(str(l.get("credit") or 0)))
			for l in lines
		),
		default=Decimal("0"),
	)


def _create_journal_entry(entry, txn_hash, settings):
	doc = frappe.new_doc("Journal Entry")
	doc.voucher_type = "Journal Entry"
	doc.company = settings.company
	doc.posting_date = str(entry["date"])[:10]
	reference = entry.get("reference")
	if reference and reference != "-":
		doc.cheque_no = reference
		doc.cheque_date = doc.posting_date
	doc.user_remark = entry.get("description")
	doc.multi_currency = 0
	doc.orion_source_type = entry.get("sourceType") or "BANK_IMPORT"
	doc.orion_txn_hash = txn_hash
	doc.orion_bank_account_no = entry.get("bankAccount")
	doc.orion_source_file = entry.get("sourceFile")

	two_dp = Decimal("0.01")
	for l in entry["lines"]:
		account = _resolve_account_name(l["accountId"])
		doc.append(
			"accounts",
			{
				"account": account,
				"debit_in_account_currency": float(Decimal(str(l.get("debit") or 0)).quantize(two_dp)),
				"credit_in_account_currency": float(Decimal(str(l.get("credit") or 0)).quantize(two_dp)),
				"cost_center": _cost_center_for(account, settings),
			},
		)

	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	return doc


def _create_bank_transaction(entry, txn_hash, je, job, settings):
	"""ERPNext Bank Transaction mirroring the statement row, submitted and
	reconciled against the JE via the payment_entries child table."""
	account_no = entry["bankAccount"]
	bank_account = frappe.db.get_value(
		"Bank Account", {"bank_account_no": account_no, "is_company_account": 1}
	)
	if not bank_account:
		frappe.throw(
			"No company Bank Account found for account no %s — cannot create Bank Transaction"
			% account_no
		)

	amount = float(_entry_max_amount(entry["lines"]).quantize(Decimal("0.01")))

	# Direction from the JE's bank GL line: bank line debited => money in.
	bank_gl_code = ACCOUNT_TO_GL.get(account_no)
	is_deposit = False
	for l in entry["lines"]:
		account = _resolve_account_name(l["accountId"])
		number = frappe.db.get_value("Account", account, "account_number")
		if number == bank_gl_code:
			is_deposit = float(l.get("debit") or 0) > 0
			break

	bt = frappe.new_doc("Bank Transaction")
	bt.date = str(entry["date"])[:10]
	bt.company = settings.company
	bt.bank_account = bank_account
	bt.currency = "IDR"
	bt.deposit = amount if is_deposit else 0
	bt.withdrawal = 0 if is_deposit else amount
	bt.description = entry.get("description")
	reference = entry.get("reference")
	if reference and reference != "-":
		bt.reference_number = reference
	bt.orion_txn_hash = txn_hash
	bt.orion_job = job.name
	bt.flags.ignore_permissions = True
	bt.insert()
	bt.submit()

	# Reconcile against the JE. Appending to payment_entries on a submitted
	# Bank Transaction and saving triggers before_update_after_submit ->
	# allocate_payment_entries + set_status (Reconciled once fully allocated).
	bt.append(
		"payment_entries",
		{
			"payment_document": "Journal Entry",
			"payment_entry": je.name,
			"allocated_amount": amount,
		},
	)
	bt.save(ignore_permissions=True)
	return bt
