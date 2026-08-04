"""Supply-chain compat handlers — mirrors bellatrix (Node) routes under
src/routes/supply-chain/ so the feynman supply-chain screens work unchanged
against ERPNext data:

  /api/supply-chain/projects         <- projects.ts        (GET list, POST,
                                        GET /:id, PATCH /:id,
                                        GET /:id/link-candidates)
  /api/supply-chain/programs         <- programs.ts        (GET list, GET /:id,
                                        POST link, DELETE /:id unlink)
  /api/supply-chain/vendors          <- vendors.ts         (GET list)
  /api/supply-chain/purchase-orders  <- purchase-orders.ts (GET list, GET /:id)
  /api/supply-chain/receipts         <- receipts.ts        (GET list, GET /:id)
  /api/supply-chain/office-receipts  <- office-receipts.ts (GET list: EMPTY)
  /api/supply-chain/spk              <- spk.ts             (GET list: EMPTY)
  /api/supply-chain/price-library    <- price-library.ts   (GET, live aggregate)

Shapes are the Node router shapes (camelCase). Like compat/accounting.py,
Decimal-backed money fields (budget, PO quantity/unitPrice/totalPrice, receipt
totalAmount/amount) go over the wire as strings — Prisma Decimals serialize
that way — while the price-library numbers stay JSON floats (the source
parseFloat()s them). Datetimes are naive ISO-8601; date-only columns ship as
midnight ISO.

Deliberate gaps, all called out inline:
  - Orion SPK and Orion Office Receipt doctypes are not built yet (wave 3);
    both lists return source-shaped empty payloads (the live spk table is
    empty anyway) and details/writes throw.
  - Purchase-order/vendor/receipt writes throw (read tranche); Projects POST /
    PATCH and Programs POST/DELETE are implemented per the Node semantics.
  - projects PATCH with programId, and any kontrakDraftFile/draft-file upload
    path, throw not-implemented (linking goes through /programs).
"""

import mimetypes
from datetime import date, datetime

import frappe

from orion.accounting import kn
from orion.compat.handle import route, split_path

SC = "/api/supply-chain"

PROJECT_STATUS_TO_LEGACY = {
	"Open": "ACTIVE",
	"Completed": "COMPLETED",
	"On hold": "ON_HOLD",
	"Cancelled": "CANCELLED",
}
LEGACY_TO_PROJECT_STATUS = {v: k for k, v in PROJECT_STATUS_TO_LEGACY.items()}

# reverse of migrate/suppliers.py TYPE_GROUP
VENDOR_TYPE_BY_GROUP = {
	"Material Supplier": "MATERIAL_SUPPLIER",
	"Subcontractor": "SUBCONTRACTOR",
	"Equipment Rental": "EQUIPMENT_RENTAL",
	"Other": "OTHER",
}

PROJECT_FIELDS = [
	"name", "project_name", "status", "expected_start_date", "expected_end_date",
	"estimated_costing", "notes", "customer", "cost_center", "creation", "modified",
	"orion_business_line", "kn_sequence", "kontrak_yymm", "lctr",
	"orion_service_type", "target_margin_pct", "orion_program",
	"client_salutation", "client_phone", "client_address", "orion_legacy_id",
]


# ── projects ────────────────────────────────────────────────────────────────


@route(SC + "/projects")
def projects(path: str, verb: str, payload: dict):
	bare, query = split_path(path)
	rest = bare[len(SC + "/projects"):].strip("/")
	parts = rest.split("/") if rest else []

	if verb == "GET" and not parts:
		return _projects_list(query)
	if verb == "POST" and not parts:
		return _project_create(payload)
	if verb == "GET" and len(parts) == 2 and parts[1] == "link-candidates":
		return _link_candidates(parts[0])
	if verb == "GET" and len(parts) == 1:
		return _project_detail(parts[0])
	if verb == "PATCH" and len(parts) == 1:
		return _project_update(parts[0], payload)
	frappe.throw(
		"%s %s is not yet implemented in compat" % (verb, bare)
	)


def _projects_list(query: dict) -> dict:
	"""projects.ts GET '': ordered knSequence desc nulls last, createdAt desc
	(kn_sequence DESC puts NULLs last on MariaDB), enriched with client,
	program stub, _count.purchaseOrders and lastDocumentCode. The Node list
	takes no filters; year/status/businessLine query params are accepted here
	as a harmless superset (ignored when absent)."""
	filters = {}
	if query.get("businessLine"):
		filters["orion_business_line"] = query["businessLine"]
	if query.get("status"):
		filters["status"] = LEGACY_TO_PROJECT_STATUS.get(query["status"], query["status"])
	if query.get("year"):
		filters["kontrak_yymm"] = ("like", "%02d%%" % (int(query["year"]) % 100))
	rows = frappe.get_all(
		"Project",
		filters=filters,
		fields=PROJECT_FIELDS,
		order_by="kn_sequence desc, creation desc",
	)
	programs = _programs_by_token()
	customers = _customers_by_name({r.customer for r in rows if r.customer})
	po_counts = _po_counts()
	last_codes = _last_document_codes()

	out = []
	for r in rows:
		p = _project_read(r, programs, customers)
		p["_count"] = {"purchaseOrders": po_counts.get(r.name, 0)}
		p["lastDocumentCode"] = last_codes.get(r.name)
		out.append(p)
	return {"projects": out}


def _project_detail(ident: str) -> dict:
	r = _project_row(ident)
	if not r:
		frappe.throw("Project not found", exc=frappe.DoesNotExistError)
	programs = _programs_by_token()
	customers = _customers_by_name({r.customer} if r.customer else set())
	p = _project_read(r, programs, customers)
	p["_count"] = {"purchaseOrders": _po_counts().get(r.name, 0)}
	return {"project": p}


def _project_create(payload: dict) -> dict:
	"""projects.ts POST — code resolution ladder:
	  1. caller `code` override           -> used as-is
	  2. structured (`lctr` + `serviceTypeCode`) -> {R|P}-KN{nn}-{YYMM}-{LCTR}-{SVC},
	     kontrakYYMM defaults to now, knSequence to the per-line per-year
	     max-scan (orion.accounting.kn.next_kn_sequence)
	  3. fallback                          -> RR-YYYY-NNN
	The Node lead->client resolution is approximated by matching a Customer on
	the exact clientName (ERPNext Lead carries no client link)."""
	name = payload.get("name")
	client_name = payload.get("clientName")
	if not name or not client_name:
		frappe.throw("name and clientName are required")

	business_line = payload.get("businessLine") or "RATUNDA_RENOVASI"
	lctr = payload.get("lctr")
	svc = payload.get("serviceTypeCode")
	kn_sequence = payload.get("knSequence")
	kontrak_yymm = payload.get("kontrakYYMM")

	if payload.get("code"):
		code = payload["code"]
	elif lctr and svc:
		if not kontrak_yymm:
			kontrak_yymm = kn.kontrak_yymm(date.today())
		if kn_sequence is None:
			effective = "POIESIS_STUDIO" if business_line == "POIESIS_STUDIO" else "RATUNDA_RENOVASI"
			kn_sequence = kn.next_kn_sequence(effective, kontrak_yymm[:2])
		code = kn.build_code(
			business_line, int(kn_sequence), kontrak_yymm[:2], kontrak_yymm[2:], lctr, svc
		)
	else:
		prefix = "RR-%s-" % date.today().year
		last = frappe.get_all(
			"Project",
			filters={"name": ("like", prefix + "%")},
			order_by="name desc",
			limit=1,
		)
		seq = int(last[0].name.split("-")[2]) + 1 if last else 1
		code = "%s%03d" % (prefix, seq)

	settings = frappe.get_cached_doc("Orion Settings")
	cc_by_line = {
		"RATUNDA_RENOVASI": settings.ratunda_cost_center,
		"POIESIS_STUDIO": settings.poiesis_cost_center,
	}

	doc = frappe.new_doc("Project")
	doc.project_name = name
	doc.status = "Open"
	doc.company = settings.company
	doc.cost_center = cc_by_line.get(business_line) or settings.shared_cost_center
	doc.customer = frappe.db.get_value("Customer", {"customer_name": client_name})
	doc.expected_start_date = _to_date(payload.get("startDate"))
	doc.expected_end_date = _to_date(payload.get("endDate"))
	doc.estimated_costing = float(payload["budget"]) if payload.get("budget") else None
	doc.notes = payload.get("description")
	doc.orion_business_line = business_line
	doc.kn_sequence = int(kn_sequence) if kn_sequence is not None else None
	doc.kontrak_yymm = kontrak_yymm
	doc.lctr = lctr
	doc.orion_service_type = svc
	doc.client_salutation = payload.get("clientSalutation") or "Pak"
	doc.client_phone = payload.get("clientPhone")
	doc.client_address = payload.get("address")
	doc.flags.ignore_permissions = True
	doc.insert(set_name=code)

	return {"project": _project_read(_project_row(code), _programs_by_token(), _customers_by_name({doc.customer} if doc.customer else set()))}


def _project_update(ident: str, payload: dict) -> dict:
	if "programId" in payload:
		frappe.throw(
			"PATCH projects with programId is not implemented in compat — "
			"link/unlink via /api/supply-chain/programs"
		)
	if "kontrakDraftFile" in payload:
		frappe.throw("kontrak draft-file upload is not implemented in compat")

	r = _project_row(ident)
	if not r:
		frappe.throw("Project not found", exc=frappe.DoesNotExistError)
	doc = frappe.get_doc("Project", r.name)

	if "name" in payload:
		doc.project_name = payload["name"]
	if "clientName" in payload:
		doc.customer = frappe.db.get_value("Customer", {"customer_name": payload["clientName"]}) or doc.customer
	if "clientSalutation" in payload:
		doc.client_salutation = payload["clientSalutation"]
	if "clientPhone" in payload:
		doc.client_phone = payload["clientPhone"]
	if "address" in payload:
		doc.client_address = payload["address"]
	if "businessLine" in payload:
		doc.orion_business_line = payload["businessLine"]
		settings = frappe.get_cached_doc("Orion Settings")
		doc.cost_center = {
			"RATUNDA_RENOVASI": settings.ratunda_cost_center,
			"POIESIS_STUDIO": settings.poiesis_cost_center,
		}.get(payload["businessLine"]) or settings.shared_cost_center
	if "status" in payload:
		doc.status = LEGACY_TO_PROJECT_STATUS.get(payload["status"], doc.status)
	if "startDate" in payload:
		doc.expected_start_date = _to_date(payload["startDate"])
	if "endDate" in payload:
		doc.expected_end_date = _to_date(payload["endDate"])
	if "budget" in payload:
		doc.estimated_costing = float(payload["budget"]) if payload["budget"] else None
	if "targetMarginPct" in payload:
		v = payload["targetMarginPct"]
		doc.target_margin_pct = float(v) if v not in (None, "") else None
	if "description" in payload:
		doc.notes = payload["description"]
	if "lctr" in payload:
		doc.lctr = payload["lctr"]
	if "knSequence" in payload:
		v = payload["knSequence"]
		doc.kn_sequence = int(v) if v is not None else None
	if "kontrakYYMM" in payload:
		doc.kontrak_yymm = payload["kontrakYYMM"]
	if "serviceTypeCode" in payload:
		doc.orion_service_type = payload["serviceTypeCode"]

	doc.flags.ignore_permissions = True
	doc.save()

	row = _project_row(doc.name)
	return {"project": _project_read(row, _programs_by_token(), _customers_by_name({row.customer} if row.customer else set()))}


def _link_candidates(ident: str) -> dict:
	"""projects.ts GET /:id/link-candidates — unlinked projects that share
	this project's lctr token but belong to the opposite business line."""
	r = _project_row(ident)
	if not r:
		frappe.throw("Project not found", exc=frappe.DoesNotExistError)
	if r.orion_program:
		return {"candidates": [], "reason": "already-linked"}
	if not r.lctr:
		return {"candidates": [], "reason": "no-token"}

	rows = frappe.get_all(
		"Project",
		filters={
			"lctr": r.lctr,
			"orion_program": ("is", "not set"),
			"orion_business_line": ("!=", r.orion_business_line),
			"name": ("!=", r.name),
		},
		fields=PROJECT_FIELDS,
		order_by="creation desc",
	)
	customers = _customers_by_name({c.customer for c in rows if c.customer})
	return {
		"candidates": [
			{
				"id": c.orion_legacy_id or c.name,
				"code": c.name,
				"name": c.project_name,
				"clientName": _client_name(c, customers),
				"businessLine": c.orion_business_line,
				"status": PROJECT_STATUS_TO_LEGACY.get(c.status, "ACTIVE"),
			}
			for c in rows
		]
	}


def _project_read(r, programs_by_token: dict, customers: dict) -> dict:
	prog = programs_by_token.get(r.orion_program) if r.orion_program else None
	customer = customers.get(r.customer) if r.customer else None
	return {
		"id": r.orion_legacy_id or r.name,
		"code": r.name,
		"name": r.project_name,
		# clientName lives on the legacy row; ERPNext keeps only the Customer
		# link (unset for imported rows — see report note on backfilling)
		"clientName": _client_name(r, customers),
		"clientSalutation": r.client_salutation or "Pak",
		"clientPhone": r.client_phone,
		"address": r.client_address,
		"businessLine": r.orion_business_line or "RATUNDA_RENOVASI",
		"status": PROJECT_STATUS_TO_LEGACY.get(r.status, "ACTIVE"),
		"startDate": _iso_date(r.expected_start_date),
		"endDate": _iso_date(r.expected_end_date),
		"budget": _dec2(r.estimated_costing),
		"targetMarginPct": r.target_margin_pct,
		"description": r.notes,
		"lctr": r.lctr,
		"knSequence": r.kn_sequence,
		"kontrakYYMM": r.kontrak_yymm,
		"serviceTypeCode": r.orion_service_type,
		"leadId": None,  # lead linkage not carried into ERPNext Project
		"clientId": (customer.orion_legacy_id or customer.name) if customer else None,
		"programId": (prog.orion_legacy_id or prog.name) if prog else None,
		"kontrakDraftFile": None,  # draft-file binaries not migrated
		"createdAt": _iso(r.creation),
		"updatedAt": _iso(r.modified),
		"client": _client_stub(r, customer),
		"program": {
			"id": prog.orion_legacy_id or prog.name,
			"token": prog.name,
			"name": prog.program_name,
		}
		if prog
		else None,
	}


def _client_name(r, customers: dict):
	customer = customers.get(r.customer) if r.customer else None
	return customer.customer_name if customer else None


def _client_stub(r, customer) -> dict | None:
	if not customer:
		return None
	return {
		"id": customer.orion_legacy_id or customer.name,
		"name": customer.customer_name,
		"salutation": r.client_salutation or "Pak",
		"phone": r.client_phone,
		"address": r.client_address,
	}


def _project_row(ident: str):
	name = frappe.db.get_value("Project", {"orion_legacy_id": ident})
	if not name and frappe.db.exists("Project", ident):
		name = ident
	if not name:
		return None
	rows = frappe.get_all("Project", filters={"name": name}, fields=PROJECT_FIELDS)
	return rows[0] if rows else None


def _programs_by_token() -> dict:
	return {
		r.name: r
		for r in frappe.get_all(
			"Orion Program",
			fields=[
				"name", "program_name", "status", "client", "site_address",
				"ratunda_project", "poiesis_project", "orion_legacy_id",
				"creation", "modified",
			],
		)
	}


def _customers_by_name(names: set) -> dict:
	if not names:
		return {}
	return {
		r.name: r
		for r in frappe.get_all(
			"Customer",
			filters={"name": ("in", list(names))},
			fields=["name", "customer_name", "orion_legacy_id"],
		)
	}


def _po_counts() -> dict:
	# v16 rejects SQL functions passed as field strings — use raw SQL
	rows = frappe.db.sql(
		"""SELECT project, COUNT(name) AS n FROM `tabPurchase Order`
		WHERE project IS NOT NULL AND project != '' GROUP BY project""",
		as_dict=True,
	)
	return {r.project: r.n for r in rows}


def _last_document_codes() -> dict:
	"""projects.ts lastDocumentCode: per project, the code of the most
	recently created document among POs, invoices, field receipts, cash
	advances (disbursements) and SPKs (doctype absent, table empty)."""
	best = {}

	def consider(project, code, created):
		if not project or not code:
			return
		cur = best.get(project)
		if not cur or (created and (cur[1] is None or created > cur[1])):
			best[project] = (code, created)

	for r in frappe.get_all(
		"Purchase Order", filters={"project": ("is", "set")},
		fields=["name", "project", "creation"],
	):
		consider(r.project, r.name, r.creation)
	for r in frappe.get_all(
		"Sales Invoice", filters={"project": ("is", "set")},
		fields=["name", "project", "creation"],
	):
		consider(r.project, r.name, r.creation)
	for r in frappe.get_all(
		"Orion Field Receipt", filters={"project": ("is", "set")},
		fields=["name", "project", "creation"],
	):
		consider(r.project, r.name, r.creation)
	for r in frappe.get_all(
		"Orion Cash Advance", filters={"project": ("is", "set")},
		fields=["name", "project", "creation"],
	):
		consider(r.project, r.name, r.creation)

	return {project: code for project, (code, _created) in best.items()}


# ── programs ────────────────────────────────────────────────────────────────


@route(SC + "/programs")
def programs(path: str, verb: str, payload: dict):
	bare, _query = split_path(path)
	rest = bare[len(SC + "/programs"):].strip("/")

	if verb == "GET" and not rest:
		return {"programs": [_program_read(r) for r in _program_rows()]}
	if verb == "GET" and "/" not in rest:
		r = _program_row(rest)
		if not r:
			frappe.throw("Program not found", exc=frappe.DoesNotExistError)
		return {"program": _program_read(r)}
	if verb == "POST" and not rest:
		return _program_link(payload)
	if verb == "DELETE" and rest and "/" not in rest:
		return _program_unlink(rest)
	frappe.throw("%s %s is not yet implemented in compat" % (verb, bare))


def _program_rows():
	return frappe.get_all(
		"Orion Program",
		fields=[
			"name", "program_name", "status", "client", "site_address",
			"ratunda_project", "poiesis_project", "orion_legacy_id",
			"creation", "modified",
		],
		order_by="creation desc",
	)


def _program_row(ident: str):
	name = frappe.db.get_value("Orion Program", {"orion_legacy_id": ident})
	if not name and frappe.db.exists("Orion Program", ident):
		name = ident
	if not name:
		return None
	rows = frappe.get_all(
		"Orion Program",
		filters={"name": name},
		fields=[
			"name", "program_name", "status", "client", "site_address",
			"ratunda_project", "poiesis_project", "orion_legacy_id",
			"creation", "modified",
		],
	)
	return rows[0] if rows else None


def _program_read(r) -> dict:
	"""programs.ts childSelect: projects as {id, code, name, businessLine,
	status} stubs, client as {id, name, salutation}."""
	child_names = [n for n in (r.ratunda_project, r.poiesis_project) if n]
	children = []
	if child_names:
		children = frappe.get_all(
			"Project",
			filters={"name": ("in", child_names)},
			fields=["name", "project_name", "orion_business_line", "status", "orion_legacy_id"],
			order_by="creation asc",
		)
	client = None
	if r.client:
		c = frappe.db.get_value(
			"Customer", r.client, ["name", "customer_name", "orion_legacy_id"], as_dict=True
		)
		if c:
			client = {
				"id": c.orion_legacy_id or c.name,
				"name": c.customer_name,
				"salutation": "Pak",  # salutation lives on the project rows
			}
	return {
		"id": r.orion_legacy_id or r.name,
		"token": r.name,
		"name": r.program_name,
		"clientId": client["id"] if client else None,
		"siteAddress": r.site_address,
		"status": r.status or "ACTIVE",
		"createdAt": _iso(r.creation),
		"updatedAt": _iso(r.modified),
		"projects": [
			{
				"id": p.orion_legacy_id or p.name,
				"code": p.name,
				"name": p.project_name,
				"businessLine": p.orion_business_line,
				"status": PROJECT_STATUS_TO_LEGACY.get(p.status, "ACTIVE"),
			}
			for p in children
		],
		"client": client,
	}


def _program_link(payload: dict) -> dict:
	"""programs.ts POST — pairs one Poiesis and one Ratunda project sharing an
	lctr token under a new Orion Program (token = lctr)."""
	poi_id = payload.get("poiesisProjectId")
	rat_id = payload.get("ratundaProjectId")
	if not poi_id or not rat_id:
		frappe.throw("poiesisProjectId and ratundaProjectId are required")

	poi = _project_row(poi_id)
	rat = _project_row(rat_id)
	if not poi or not rat:
		frappe.throw("Project not found", exc=frappe.DoesNotExistError)
	if poi.orion_business_line != "POIESIS_STUDIO" or rat.orion_business_line != "RATUNDA_RENOVASI":
		frappe.throw("Expected one Poiesis and one Ratunda project")
	if poi.orion_program or rat.orion_program:
		frappe.throw("One of the projects is already linked to a program")
	if not poi.lctr or poi.lctr != rat.lctr:
		frappe.throw("Projects must share the same engagement token (lctr)")

	token = poi.lctr
	if frappe.db.exists("Orion Program", token):
		frappe.throw("A program already exists for this engagement")

	customers = _customers_by_name({n for n in (rat.customer, poi.customer) if n})
	client_name = _client_name(rat, customers) or _client_name(poi, customers)

	doc = frappe.new_doc("Orion Program")
	doc.token = token
	doc.program_name = "%s (%s)" % (client_name or token, token)
	doc.status = "ACTIVE"
	doc.client = rat.customer or poi.customer
	doc.site_address = rat.client_address or poi.client_address
	doc.ratunda_project = rat.name
	doc.poiesis_project = poi.name
	doc.flags.ignore_permissions = True
	doc.insert()

	for project in (rat.name, poi.name):
		frappe.db.set_value("Project", project, "orion_program", token, update_modified=False)

	return {"program": _program_read(_program_row(token))}


def _program_unlink(ident: str) -> dict:
	"""programs.ts DELETE — detach both children, remove the program."""
	r = _program_row(ident)
	if not r:
		frappe.throw("Program not found", exc=frappe.DoesNotExistError)
	for project in (r.ratunda_project, r.poiesis_project):
		if project:
			frappe.db.set_value("Project", project, "orion_program", None, update_modified=False)
	frappe.delete_doc("Orion Program", r.name, ignore_permissions=True)
	return {"success": True}


# ── vendors ─────────────────────────────────────────────────────────────────


@route(SC + "/vendors")
def vendors(path: str, verb: str, payload: dict):
	"""vendors.ts GET '' -> {vendors: [...]} ordered code asc, with
	_count.purchaseOrders. phone/address/bank fields were never stored on
	Supplier (the live vendors table is empty) and read back null. Writes
	throw — read tranche."""
	bare, _query = split_path(path)
	if verb != "GET" or bare.strip("/") != (SC + "/vendors").strip("/"):
		frappe.throw("%s %s is not yet implemented in compat" % (verb, bare))

	rows = frappe.get_all(
		"Supplier",
		fields=[
			"name", "supplier_name", "supplier_group", "disabled",
			"orion_vendor_code", "orion_legacy_id", "creation", "modified",
		],
		order_by="orion_vendor_code asc, name asc",
	)
	counts = {
		r.supplier: r.n
		for r in frappe.db.sql(
			"""SELECT supplier, COUNT(name) AS n FROM `tabPurchase Order`
			GROUP BY supplier""",
			as_dict=True,
		)
	}
	return {
		"vendors": [
			{
				"id": r.orion_legacy_id or r.name,
				"code": r.orion_vendor_code,
				"name": r.supplier_name,
				"type": VENDOR_TYPE_BY_GROUP.get(r.supplier_group, "OTHER"),
				"phone": None,
				"address": None,
				"bankName": None,
				"bankAccount": None,
				"accountName": None,
				"notes": None,
				"isActive": not r.disabled,
				"createdAt": _iso(r.creation),
				"updatedAt": _iso(r.modified),
				"_count": {"purchaseOrders": counts.get(r.name, 0)},
			}
			for r in rows
		]
	}


# ── purchase orders ─────────────────────────────────────────────────────────

PO_FIELDS = [
	"name", "supplier", "project", "status", "docstatus",
	"transaction_date", "schedule_date", "creation", "modified",
	"orion_po_type", "orion_marketplace", "orion_online_order_id",
	"orion_status", "orion_received_date", "orion_cancelled_date",
	"orion_cancel_reason", "orion_notes", "orion_legacy_id",
]


@route(SC + "/purchase-orders")
def purchase_orders(path: str, verb: str, payload: dict):
	bare, _query = split_path(path)
	rest = bare[len(SC + "/purchase-orders"):].strip("/")
	if verb == "GET" and not rest:
		return _po_list()
	if verb == "GET" and "/" not in rest:
		return _po_detail(rest)
	frappe.throw("%s %s is not yet implemented in compat — read tranche" % (verb, bare))


def _po_list() -> dict:
	"""purchase-orders.ts GET '': createdAt desc, project/vendor stubs,
	items reduced to [{totalPrice}]."""
	rows = frappe.get_all("Purchase Order", fields=PO_FIELDS, order_by="creation desc")
	amounts = {}
	if rows:
		for i in frappe.get_all(
			"Purchase Order Item",
			filters={"parenttype": "Purchase Order", "parent": ("in", [r.name for r in rows])},
			fields=["parent", "amount"],
			order_by="parent asc, idx asc",
		):
			amounts.setdefault(i.parent, []).append({"totalPrice": _dec2(i.amount) or "0"})
	projects = _project_stubs({r.project for r in rows if r.project})
	suppliers = _supplier_rows({r.supplier for r in rows if r.supplier})

	out = []
	for r in rows:
		v = suppliers.get(r.supplier)
		out.append(
			{
				"id": r.orion_legacy_id or r.name,
				"number": r.name,
				"status": _po_status(r),
				"poType": r.orion_po_type or "PO",
				"orderDate": _iso_date(r.transaction_date),
				# importer sets schedule_date = expectedDate or orderDate, so a
				# source-null expectedDate reads back as the order date
				"expectedDate": _iso_date(r.schedule_date),
				"receivedDate": _iso_date(r.orion_received_date),
				"cancelledDate": _iso_date(r.orion_cancelled_date),
				"notes": r.orion_notes,
				"createdAt": _iso(r.creation),
				"marketplace": r.orion_marketplace or None,
				"onlineOrderId": r.orion_online_order_id,
				"project": projects.get(r.project),
				"vendor": {
					"id": v.orion_legacy_id or v.name,
					"code": v.orion_vendor_code,
					"name": v.supplier_name,
					"type": VENDOR_TYPE_BY_GROUP.get(v.supplier_group, "OTHER"),
				}
				if v
				else None,
				"items": amounts.get(r.name, []),
			}
		)
	return {"orders": out}


def _po_detail(ident: str) -> dict:
	"""purchase-orders.ts GET /:id: full project + vendor, items asc."""
	name = frappe.db.get_value("Purchase Order", {"orion_legacy_id": ident})
	if not name and frappe.db.exists("Purchase Order", ident):
		name = ident
	if not name:
		frappe.throw("Purchase order not found", exc=frappe.DoesNotExistError)
	rows = frappe.get_all("Purchase Order", filters={"name": name}, fields=PO_FIELDS)
	r = rows[0]
	order_id = r.orion_legacy_id or r.name

	items = frappe.get_all(
		"Purchase Order Item",
		filters={"parenttype": "Purchase Order", "parent": name},
		fields=[
			"name", "description", "item_name", "qty", "rate", "amount",
			"orion_unit", "orion_notes", "creation",
		],
		order_by="idx asc",
	)
	project = None
	if r.project:
		prow = _project_row(r.project)
		if prow:
			project = _project_read(
				prow, _programs_by_token(),
				_customers_by_name({prow.customer} if prow.customer else set()),
			)
	v = _supplier_rows({r.supplier}).get(r.supplier)

	return {
		"order": {
			"id": order_id,
			"number": r.name,
			"status": _po_status(r),
			"poType": r.orion_po_type or "PO",
			"marketplace": r.orion_marketplace or None,
			"onlineOrderId": r.orion_online_order_id,
			"orderDate": _iso_date(r.transaction_date),
			"expectedDate": _iso_date(r.schedule_date),
			"receivedDate": _iso_date(r.orion_received_date),
			"cancelledDate": _iso_date(r.orion_cancelled_date),
			"cancelReason": r.orion_cancel_reason,
			"notes": r.orion_notes,
			"createdAt": _iso(r.creation),
			"updatedAt": _iso(r.modified),
			"project": project,
			"vendor": {
				"id": v.orion_legacy_id or v.name,
				"code": v.orion_vendor_code,
				"name": v.supplier_name,
				"type": VENDOR_TYPE_BY_GROUP.get(v.supplier_group, "OTHER"),
				"isActive": not v.disabled,
			}
			if v
			else None,
			"items": [
				{
					"id": i.name,
					"purchaseOrderId": order_id,
					"description": (i.description or i.item_name or "").strip(),
					"unit": i.orion_unit,
					"quantity": "%.3f" % float(i.qty or 0),
					"unitPrice": _dec2(i.rate) or "0.00",
					"totalPrice": _dec2(i.amount) or "0.00",
					"notes": i.orion_notes,
					"createdAt": _iso(i.creation),
				}
				for i in items
			],
		}
	}


def _po_status(r) -> str:
	"""orion_status preserves the legacy status verbatim for imported rows;
	derive from ERPNext state otherwise."""
	if r.orion_status:
		return r.orion_status
	if r.docstatus == 2:
		return "CANCELLED"
	if r.docstatus == 1:
		return "RECEIVED" if r.status == "Closed" else "ORDERED"
	return "DRAFT"


def _project_stubs(names: set) -> dict:
	if not names:
		return {}
	return {
		r.name: {
			"id": r.orion_legacy_id or r.name,
			"code": r.name,
			"name": r.project_name,
			"lctr": r.lctr,
		}
		for r in frappe.get_all(
			"Project",
			filters={"name": ("in", list(names))},
			fields=["name", "project_name", "lctr", "orion_legacy_id"],
		)
	}


def _supplier_rows(names: set) -> dict:
	if not names:
		return {}
	return {
		r.name: r
		for r in frappe.get_all(
			"Supplier",
			filters={"name": ("in", list(names))},
			fields=[
				"name", "supplier_name", "supplier_group", "disabled",
				"orion_vendor_code", "orion_legacy_id",
			],
		)
	}


# ── field receipts (nota lapangan) ──────────────────────────────────────────

FR_FIELDS = [
	"name", "project", "cash_advance", "status", "store_name", "receipt_date",
	"receipt_ref", "notes", "total_amount", "orion_legacy_id", "creation", "modified",
]


@route(SC + "/receipts")
def receipts(path: str, verb: str, payload: dict):
	bare, query = split_path(path)
	rest = bare[len(SC + "/receipts"):].strip("/")
	if verb == "GET" and not rest:
		return _receipts_list(query)
	if verb == "GET" and "/" not in rest:
		return _receipt_detail(rest)
	frappe.throw("%s %s is not yet implemented in compat — read tranche" % (verb, bare))


def _receipts_list(query: dict) -> dict:
	"""receipts.ts GET ?projectId=&disbursementId=&status= — createdAt desc,
	project/disbursement stubs, items reduced to [{amount}]."""
	filters = {}
	if query.get("projectId"):
		project = frappe.db.get_value("Project", {"orion_legacy_id": query["projectId"]}) or query["projectId"]
		filters["project"] = project
	if query.get("disbursementId"):
		ca = frappe.db.get_value(
			"Orion Cash Advance", {"orion_legacy_id": query["disbursementId"]}
		) or query["disbursementId"]
		filters["cash_advance"] = ca
	if query.get("status") and query["status"] != "ALL":
		filters["status"] = query["status"]

	rows = frappe.get_all(
		"Orion Field Receipt", filters=filters, fields=FR_FIELDS, order_by="creation desc"
	)
	amounts = {}
	if rows:
		for i in frappe.get_all(
			"Orion Field Receipt Item",
			filters={"parenttype": "Orion Field Receipt", "parent": ("in", [r.name for r in rows])},
			fields=["parent", "amount"],
			order_by="parent asc, idx asc",
		):
			amounts.setdefault(i.parent, []).append({"amount": _dec2(i.amount) or "0"})
	projects = _project_stubs({r.project for r in rows if r.project})
	cas = _ca_stubs({r.cash_advance for r in rows if r.cash_advance})

	return {
		"receipts": [
			dict(
				_receipt_read(r),
				project=projects.get(r.project),
				disbursement=cas.get(r.cash_advance),
				items=amounts.get(r.name, []),
			)
			for r in rows
		]
	}


def _receipt_detail(ident: str) -> dict:
	name = frappe.db.get_value("Orion Field Receipt", {"orion_legacy_id": ident})
	if not name and frappe.db.exists("Orion Field Receipt", ident):
		name = ident
	if not name:
		frappe.throw("Not found", exc=frappe.DoesNotExistError)
	r = frappe.get_all("Orion Field Receipt", filters={"name": name}, fields=FR_FIELDS)[0]
	receipt_id = r.orion_legacy_id or r.name

	items = frappe.get_all(
		"Orion Field Receipt Item",
		filters={"parenttype": "Orion Field Receipt", "parent": name},
		fields=[
			"name", "description", "category", "unit", "quantity", "unit_price",
			"amount", "notes", "sort_order", "creation",
		],
		order_by="sort_order asc, idx asc",
	)
	out = _receipt_read(r)
	out["project"] = _project_stubs({r.project}).get(r.project) if r.project else None
	out["disbursement"] = _ca_stubs({r.cash_advance}).get(r.cash_advance) if r.cash_advance else None
	out["items"] = [
		{
			"id": i.name,
			"fieldReceiptId": receipt_id,
			"description": i.description,
			"category": i.category,
			"unit": i.unit,
			"quantity": "%.3f" % float(i.quantity or 0),
			"unitPrice": _dec2(i.unit_price) or "0.00",
			"amount": _dec2(i.amount) or "0.00",
			"notes": i.notes,
			"sortOrder": i.sort_order or 0,
			"createdAt": _iso(i.creation),
		}
		for i in items
	]
	# scan binaries live as private File attachments (attach_field_receipt_files.py
	# backfills them from the legacy uploads tree); files_note stays as the audit
	# trail of the original local paths.
	out["files"] = _receipt_files(name, receipt_id)
	return {"receipt": out}


def _receipt_read(r) -> dict:
	projects = _project_stubs({r.project}) if r.project else {}
	return {
		"id": r.orion_legacy_id or r.name,
		"code": r.name,
		"projectId": (projects.get(r.project) or {}).get("id") if r.project else None,
		"disbursementId": frappe.db.get_value("Orion Cash Advance", r.cash_advance, "orion_legacy_id") or r.cash_advance
		if r.cash_advance
		else None,
		"storeName": r.store_name,
		"receiptDate": _iso_date(r.receipt_date),
		"receiptRef": r.receipt_ref,
		"notes": r.notes,
		"status": r.status or "DRAFT",
		"totalAmount": _dec2(r.total_amount) or "0.00",
		"createdAt": _iso(r.creation),
		"updatedAt": _iso(r.modified),
	}


def _ca_stubs(names: set) -> dict:
	if not names:
		return {}
	return {
		r.name: {
			"id": r.orion_legacy_id or r.name,
			"code": r.name,
			"title": r.title,
			"totalAmount": _dec2(r.total_amount) or "0.00",
			"status": r.status,
		}
		for r in frappe.get_all(
			"Orion Cash Advance",
			filters={"name": ("in", list(names))},
			fields=["name", "title", "total_amount", "status", "orion_legacy_id"],
		)
	}


# ── office receipts / SPK — doctypes not built yet (wave 3) ─────────────────


@route(SC + "/office-receipts")
def office_receipts(path: str, verb: str, payload: dict):
	"""The Orion Office Receipt doctype was NOT built in wave 2; until it
	lands, the list is served source-shaped but empty so the feynman screen
	renders (office-receipts.ts GET '' -> {receipts: []})."""
	bare, _query = split_path(path)
	rest = bare[len(SC + "/office-receipts"):].strip("/")
	if verb == "GET" and not rest:
		return {"receipts": []}
	if verb == "GET" and "/" not in rest:
		frappe.throw("Not found", exc=frappe.DoesNotExistError)
	frappe.throw(
		"%s %s is not yet implemented in compat — Orion Office Receipt doctype pending"
		% (verb, bare)
	)


@route(SC + "/spk")
def spk(path: str, verb: str, payload: dict):
	"""The Orion SPK doctype was NOT built in wave 2 (and the live spk table
	is empty). Serve the source-shaped empty list (spk.ts GET '' ->
	{spks: []}); everything else throws until the doctype lands."""
	bare, _query = split_path(path)
	rest = bare[len(SC + "/spk"):].strip("/")
	if verb == "GET" and not rest:
		return {"spks": []}
	if verb == "GET" and "/" not in rest:
		frappe.throw("SPK not found", exc=frappe.DoesNotExistError)
	frappe.throw(
		"%s %s is not yet implemented in compat — Orion SPK doctype pending" % (verb, bare)
	)


# ── price library ───────────────────────────────────────────────────────────


@route(SC + "/price-library")
def price_library(path: str, verb: str, payload: dict):
	"""price-library.ts GET ?category=&q= — there is no table behind this in
	the source either: it aggregates field_receipt_items of VERIFIED receipts
	grouped by (description, unit, category) with last/avg/min/max unit
	price, count, lastSeen and the last 3 project refs per (description,
	unit). Re-derived here live from Orion Field Receipt (+ items); numbers
	stay JSON floats like the source (it parseFloat()s its SQL strings)."""
	bare, query = split_path(path)
	if verb != "GET" or bare.strip("/") != (SC + "/price-library").strip("/"):
		frappe.throw("%s %s is not yet implemented in compat" % (verb, bare))

	category = query.get("category")
	q = (query.get("q") or "").lower()

	receipts = {
		r.name: r
		for r in frappe.get_all(
			"Orion Field Receipt",
			filters={"status": "VERIFIED"},
			fields=["name", "receipt_date", "project"],
		)
	}
	if not receipts:
		return {"items": []}
	items = frappe.get_all(
		"Orion Field Receipt Item",
		filters={"parenttype": "Orion Field Receipt", "parent": ("in", list(receipts))},
		fields=["parent", "description", "category", "unit", "unit_price"],
	)

	project_lctr = {
		p.name: (p.lctr or p.name)
		for p in frappe.get_all("Project", fields=["name", "lctr"])
	}

	groups = {}  # (description, unit, category) -> rows
	refs = {}  # (description, unit) -> {project code: sort key}
	for i in items:
		fr = receipts[i.parent]
		if category and category != "ALL" and i.category != category:
			continue
		if q and q not in (i.description or "").lower():
			continue
		key = (i.description, i.unit, i.category)
		groups.setdefault(key, []).append((fr.receipt_date, i.parent, float(i.unit_price or 0)))
		if fr.project:
			refs.setdefault((i.description, i.unit), set()).add(fr.project)

	out = []
	for (description, unit, cat), rows in sorted(
		groups.items(), key=lambda g: (g[0][0] or "", g[0][1] or "", g[0][2] or "")
	):
		rows.sort(key=lambda t: (t[0] or date.min, t[1]), reverse=True)
		prices = [t[2] for t in rows]
		projects = sorted(refs.get((description, unit), set()), reverse=True)[:3]
		out.append(
			{
				"description": description,
				"unit": unit,
				"category": cat,
				"lastPrice": prices[0],
				"avgPrice": round(sum(prices) / len(prices), 2),
				"minPrice": min(prices),
				"maxPrice": max(prices),
				"count": len(prices),
				"lastSeen": _iso_date(rows[0][0]),
				"projectRefs": [project_lctr.get(p, p) for p in projects],
			}
		)
	return {"items": out}


# ── shared helpers ──────────────────────────────────────────────────────────


# Extensions the legacy nota uploader accepted that mimetypes may not know.
_EXTRA_MIME = {".heic": "image/heic", ".heif": "image/heif", ".webp": "image/webp"}


def _mime_from_name(filename: str) -> str:
	if not filename or "." not in filename:
		return "application/octet-stream"
	ext = "." + filename.rsplit(".", 1)[1].lower()
	return _EXTRA_MIME.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _receipt_files(fr_name: str, receipt_id: str) -> list:
	"""Scan attachments for a field receipt, shaped like the legacy
	FieldReceiptFile wire object (receipts.ts). `url` is the Frappe file_url the
	SPA loads directly; private files are gated on Orion Field Receipt read, which
	every Orion role holds."""
	rows = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Orion Field Receipt", "attached_to_name": fr_name},
		fields=["name", "file_name", "file_url", "file_size", "creation"],
		order_by="creation asc, file_name asc",
	)
	return [
		{
			"id": f.name,
			"fieldReceiptId": receipt_id,
			"filename": f.file_name,
			"originalName": f.file_name,
			"mimeType": _mime_from_name(f.file_name),
			"fileSize": f.file_size or 0,
			"url": f.file_url,
			"gdriveSynced": False,
			"uploadedAt": _iso(f.creation),
		}
		for f in rows
	]


def _dec2(value) -> str | None:
	"""Currency/Decimal field -> 2dp string (Prisma-Decimal wire shape)."""
	if value is None:
		return None
	return "%.2f" % float(value)


def _to_date(value):
	"""ISO timestamp/date string -> YYYY-MM-DD (or None)."""
	return value[:10] if value else None


def _iso(dt) -> str | None:
	if not dt:
		return None
	if isinstance(dt, str):
		dt = frappe.utils.get_datetime(dt)
	return dt.isoformat()


def _iso_date(d) -> str | None:
	"""Date columns in the source's DateTime wire shape: midnight ISO."""
	if not d:
		return None
	if isinstance(d, datetime):
		d = d.date()
	return "%sT00:00:00" % d
