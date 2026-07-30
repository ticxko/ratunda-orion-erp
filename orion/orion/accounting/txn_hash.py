"""Deterministic deduplication hash for bank-import transactions.

Port of `app/services/accounting/txn_hash.py` from Bellatrix Python (itself a
port of `src/lib/accounting/txn-hash.ts` from Bellatrix Node). The output MUST
be byte-for-byte identical for matching inputs so that hashes already stored
on rows imported by Node continue to match after the cutover.

Priority:
  1. If a real bank reference exists (non-empty, non-"-"): use bankAccount + reference.
     Bank reference numbers (BI Fast ID, etc.) are globally unique per account.
  2. Fallback: bankAccount + date + amount + normalised description.
     Normalisation strips punctuation and truncates to avoid OCR/AI extraction diffs.
"""

import re

_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_MULTI_SPACE_RE = re.compile(r"\s+")


def has_real_ref(reference: str | None) -> bool:
	if reference is None:
		return False
	ref = reference.strip()
	return ref != "" and ref != "-"


def compute_txn_hash(
	bank_account: str,
	date: str,  # YYYY-MM-DD or longer ISO; only first 10 chars used
	reference: str | None,
	amount: float,
	description: str,
) -> str:
	ref = (reference or "").strip()
	if ref and ref != "-":
		return f"{bank_account}|ref:{ref}"

	date_key = date[:10]
	desc = description.lower().strip()
	desc = _PUNCT_RE.sub("", desc)
	desc = _MULTI_SPACE_RE.sub(" ", desc)
	desc = desc[:50]
	# Match Node's `${amount}` JS coercion: integers render with no fractional
	# part. We mirror that by formatting whole numbers as int and others
	# via the default float repr — but in practice amount comes through as a
	# number; JS Number.toString uses the same canonical form Python's int/float
	# do for whole numbers. Use int when no fractional component present.
	if isinstance(amount, int) or (
		isinstance(amount, float) and amount.is_integer()
	):
		amount_str = str(int(amount))
	else:
		amount_str = repr(amount)
	return f"{bank_account}|{date_key}|{amount_str}|{desc}"
