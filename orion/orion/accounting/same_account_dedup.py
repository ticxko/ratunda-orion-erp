"""Same-account duplicate detection for bank-statement imports.

Port of `app/services/accounting/same_account_dedup.py` from Bellatrix Python.

`txn_hash` (see txn_hash.py) dedups on the bank reference when one is present,
falling back to bank|date|amount|description. That breaks when the SAME bank
transaction is parsed out of two different statement PDFs with two different
references — the hashes differ, so Layer 1 finds nothing, and Layer 2 only
looks at OTHER bank accounts (inter-account transfers). Observed in production:

  * Kopra 21 Jun 2026, Rp 7.100.000 — single-day export put the voucher token
    `R-CAO05-KN02-26II-L` from the narrative in `reference`; the month export
    carried the real bank ref `0202606202002641255`. Both imported.
  * Jago 4 Jun 2026, Rp 2.000.000 — one export carried ref `014554865893`, a
    later wider-window export carried none, so it fell to the description hash.
    Both imported.

This module closes that hole by matching on what cannot vary between exports:
account + value date + amount + direction.

The matcher is COUNT-AWARE, and that is the whole point. Naively flagging every
(date, amount, direction) collision would drop legitimate repeats — the Kopra
statement genuinely carries fifteen separate Rp 5.000 transfer fees on
2026-05-08. Instead we compare how many occurrences the ledger already holds
against how many the batch contains, and only the excess is flagged. Fifteen in
the ledger and fifteen in the batch → all fifteen are duplicates. Fifteen in the
ledger and sixteen in the batch → one is genuinely new.

A flagged transaction is skipped by default in the UI but stays visible and
re-includable, so a false positive costs the operator a click rather than
silently losing a transaction.
"""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

# "IN" = money into the account (bank GL debited); "OUT" = money out (credited).
Direction = str

DupKey = tuple[str, str, Direction]


def amount_key(amount: Decimal | float | int) -> str:
	"""Canonical 2dp string so 7100000, 7100000.0 and Decimal('7100000.00') agree."""
	return str(Decimal(str(amount)).quantize(Decimal("0.01")))


def dup_key(date_key: str, amount: Decimal | float | int, direction: Direction) -> DupKey:
	return (date_key[:10], amount_key(amount), direction)


@dataclass(frozen=True)
class BatchTxn:
	"""One parsed statement line awaiting import."""

	idx: int
	date_key: str
	amount: Decimal | float | int
	direction: Direction
	already_duplicate: bool  # flagged by an earlier dedup layer


def find_same_account_duplicates(
	batch: list[BatchTxn],
	ledger_counts: "Counter[DupKey]",
) -> set[int]:
	"""Return the `idx` of batch transactions already present in the ledger.

	`ledger_counts` holds occurrences ALREADY posted for this bank account,
	keyed by (date, amount, direction). Transactions the earlier layers already
	caught consume their ledger slot first, so this layer never double-counts a
	match that Layer 1 or 2 made on the same underlying transaction.
	"""
	remaining = Counter(ledger_counts)

	for tx in batch:
		if tx.already_duplicate:
			key = dup_key(tx.date_key, tx.amount, tx.direction)
			if remaining[key] > 0:
				remaining[key] -= 1

	flagged: set[int] = set()
	for tx in batch:
		if tx.already_duplicate:
			continue
		key = dup_key(tx.date_key, tx.amount, tx.direction)
		if remaining[key] > 0:
			remaining[key] -= 1
			flagged.add(tx.idx)

	return flagged
