"""Bank-statement compat handlers — mirrors app/routers/accounting/bank_statement.py.

Routes served through the JSON gateway (orion.compat.handle):

  GET  /api/accounting/bank-statement/jobs/<id>          -> job status/result
  GET  /api/accounting/bank-statement/inbox              -> auto-import review inbox
  POST /api/accounting/bank-statement/jobs/<id>/reviewed -> mark reviewed; when the
       payload carries {"entries": [...]} (the accepted rows, in the exact shape
       feynman's BankStatementImport.vue builds for the legacy
       POST /api/accounting/journal-entries call), it FIRST dual-writes each row
       as a submitted Journal Entry + reconciled Bank Transaction
       (orion.accounting.bank_statement.approve), then flags the job reviewed.
       Response is the source's {"ok", "id", "reviewed"} plus additive
       {"created", "skipped", "skippedList"} when entries were posted.

POST /api/accounting/bank-statement/parse CANNOT go through the JSON gateway
(multipart file upload). feynman's apiRaw POST for it must be re-pointed at:

	POST /api/method/orion.compat.bank_statement.upload
	multipart form: file=<pdf>, source=manual|auto

which returns Frappe's envelope {"message": {"jobId": "<job name>"}} — the
apiRaw rewire unwraps `message` (same unwrap api.ts applies to handle()).
The scheduled Kopra importer authenticates with the orion-ingest bot user's
API key (Authorization: token key:secret) against the same endpoint.
"""

import json

import frappe

from orion.compat.handle import route, split_path

PREFIX = "/api/accounting/bank-statement"
JOB_DOCTYPE = "Orion Bank Statement Job"


@route(PREFIX)
def bank_statement(path: str, verb: str, payload: dict):
	bare, _query = split_path(path)
	rest = bare[len(PREFIX):].strip("/")
	parts = [p for p in rest.split("/") if p]

	if verb == "GET" and parts == ["inbox"]:
		return _inbox()
	if verb == "GET" and len(parts) == 2 and parts[0] == "jobs":
		return _get_job(parts[1])
	if verb == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "reviewed":
		return _mark_reviewed(parts[1], payload)
	if verb == "POST" and parts == ["parse"]:
		frappe.throw(
			"POST %s/parse carries a file and cannot go through the JSON gateway — "
			"POST multipart to /api/method/orion.compat.bank_statement.upload instead"
			% PREFIX
		)
	frappe.throw("No compat handler for %s %s" % (verb, bare), exc=frappe.DoesNotExistError)


def _resolve_job(ident: str) -> str:
	"""Job by name first, then by orion_legacy_id (archived source jobs)."""
	if frappe.db.exists(JOB_DOCTYPE, ident):
		return ident
	name = frappe.db.get_value(JOB_DOCTYPE, {"orion_legacy_id": ident})
	if not name:
		frappe.throw("Job not found", exc=frappe.DoesNotExistError)
	return name


def _get_job(ident: str) -> dict:
	"""GET /jobs/<id> — source shape: status/fileName/source/reviewed/result/error.
	`result` goes over the wire as a JSON object (the doctype stores a string)."""
	job = frappe.db.get_value(
		JOB_DOCTYPE,
		_resolve_job(ident),
		["status", "file_name", "source", "reviewed", "result", "error"],
		as_dict=True,
	)
	return {
		"status": job.status,
		"fileName": job.file_name,
		"source": job.source,
		"reviewed": bool(job.reviewed),
		"result": json.loads(job.result) if job.result else None,
		"error": job.error,
	}


def _inbox() -> list:
	"""GET /inbox — auto-imported statements awaiting review, newest first."""
	jobs = frappe.get_all(
		JOB_DOCTYPE,
		filters={"source": "auto", "reviewed": 0},
		fields=[
			"name", "file_name", "status", "source", "reviewed", "error",
			"creation", "result", "orion_legacy_id",
		],
		order_by="creation desc",
	)
	return [_inbox_summary(j) for j in jobs]


def _inbox_summary(job) -> dict:
	result = json.loads(job.result) if job.result else {}
	info = result.get("info") or {}
	txns = result.get("transactions") or []
	recon = result.get("reconciliation") or {}
	return {
		"id": job.name,
		"fileName": job.file_name,
		"status": job.status,
		"source": job.source,
		"reviewed": bool(job.reviewed),
		"error": job.error,
		"createdAt": job.creation.isoformat() if job.creation else None,
		"accountNo": info.get("accountNo"),
		"period": info.get("period"),
		"format": result.get("format"),
		"transactionCount": len(txns),
		"duplicateCount": result.get("duplicateCount"),
		"reconciliationOk": recon.get("ok"),
	}


def _mark_reviewed(ident: str, payload: dict) -> dict:
	from orion.accounting.bank_statement import approve

	name = _resolve_job(ident)
	out = {}
	entries = (payload or {}).get("entries")
	if entries:
		out = approve(name, entries)
	frappe.db.set_value(
		JOB_DOCTYPE,
		name,
		{"reviewed": 1, "reviewed_at": frappe.utils.now_datetime()},
	)
	response = {"ok": True, "id": name, "reviewed": True}
	response.update(out)
	return response


@frappe.whitelist(methods=["POST"])
def upload():
	"""Multipart upload endpoint (replaces POST /parse) — the JSON gateway
	cannot carry files. Accepts form fields `file` (the PDF) and `source`.
	Returns {"jobId": <job name>} inside Frappe's {"message": ...} envelope."""
	from orion.accounting.bank_statement import create_job

	if frappe.session.user in ("Guest", "", None):
		raise frappe.AuthenticationError

	if not frappe.conf.get("anthropic_api_key"):
		frappe.throw("ANTHROPIC_API_KEY tidak dikonfigurasi")

	file = frappe.request.files.get("file")
	if file is None or not file.filename:
		frappe.throw("No PDF file provided")

	source = frappe.form_dict.get("source") or "manual"
	try:
		job = create_job(
			file_name=file.filename,
			content=file.stream.read(),
			source=source,
		)
	except ValueError as e:
		# pdf_password.maybe_decrypt: encrypted PDF with no working password
		frappe.throw(str(e))
	return {"jobId": job.name}
