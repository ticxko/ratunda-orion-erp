"""Auto-decrypt password-protected bank statement PDFs.

Port of `app/services/accounting/pdf_password.py` from Bellatrix Python.

Anthropic rejects encrypted PDFs ("The PDF specified is password protected"),
so we have to unlock them server-side before forwarding. Passwords are tried
in two phases: filename-pattern match (high confidence) first, then a
fallback sweep across all registered passwords (handles renamed files).

Pure module — no frappe import, unit-testable outside a bench.
"""

import io
import re

from pypdf import PdfReader, PdfWriter

# Registered passwords. Each entry is (regex matched against filename, password, label).
# Mandiri Livin password convention: holder's date of birth (DDMMYYYY).
# Mandiri Livin filename convention: e-Statement_XXXXXXXXX<last4>_<date-range>.pdf
# where <last4> = last 4 digits of the account number.
_FILENAME_PASSWORDS: list[tuple[re.Pattern, str, str]] = [
	# Amita Ratih ("Mayang") — Livin 1660007212202
	(re.compile(r"X+2202_", re.IGNORECASE), "02071985", "Amita Ratih Livin"),
	# Lastiko Harmantyo — Livin 1640005991353
	(re.compile(r"X+1353_", re.IGNORECASE), "19011985", "Lastiko Harmantyo Livin"),
]


def _candidate_passwords(filename: str) -> list[str]:
	"""Return passwords to try, ordered by confidence.

	Phase 1: filename-pattern matches (high confidence — fast path).
	Phase 2: every other registered password (fallback for renamed files).
	"""
	seen: set[str] = set()
	candidates: list[str] = []

	# Phase 1: filename pattern matches first
	for pattern, password, _label in _FILENAME_PASSWORDS:
		if pattern.search(filename) and password not in seen:
			candidates.append(password)
			seen.add(password)

	# Phase 2: all remaining registered passwords as fallback
	for _pattern, password, _label in _FILENAME_PASSWORDS:
		if password not in seen:
			candidates.append(password)
			seen.add(password)

	return candidates


def maybe_decrypt(pdf_bytes: bytes, filename: str) -> bytes:
	"""Return decrypted PDF bytes if the input is encrypted and we have a
	matching password; otherwise return the input unchanged.

	Raises ValueError if the PDF is encrypted and no registered password unlocks it.
	"""
	try:
		reader = PdfReader(io.BytesIO(pdf_bytes))
	except Exception:
		return pdf_bytes

	if not reader.is_encrypted:
		return pdf_bytes

	# Owner-permission-only encryption (e.g. BCA Xpresi): empty user password unlocks.
	# Anthropic accepts these natively, so we don't need to rewrite.
	if reader.decrypt("") != 0:
		return pdf_bytes

	candidates = _candidate_passwords(filename)
	if not candidates:
		raise ValueError(
			f"PDF '{filename}' is password-protected and no passwords are registered. "
			f"Add an entry to pdf_password._FILENAME_PASSWORDS."
		)

	for password in candidates:
		# Fresh reader per attempt — pypdf state can get stuck after a failed decrypt.
		reader = PdfReader(io.BytesIO(pdf_bytes))
		if reader.decrypt(password) != 0:
			writer = PdfWriter()
			for page in reader.pages:
				writer.add_page(page)
			out = io.BytesIO()
			writer.write(out)
			return out.getvalue()

	raise ValueError(
		f"PDF '{filename}' is password-protected and none of the "
		f"{len(candidates)} registered passwords unlocked it."
	)
