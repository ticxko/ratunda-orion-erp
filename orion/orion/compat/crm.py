"""CRM compat handlers — mirrors the bellatrix (Node) routes so the feynman CRM
screens work unchanged against ERPNext data:

  /api/leads          <- src/routes/crm/index.ts leads   (GET list, POST,
                         PATCH /:id, DELETE /:id)
  /api/clients        <- src/routes/crm/index.ts clients (GET list, POST,
                         GET /:id, PATCH /:id, DELETE /:id)
  /api/contracts      <- src/routes/crm/index.ts contracts(GET list, POST,
                         GET /kn-next, GET /lctr-availability, GET /:id,
                         PATCH /:id, POST /analyze -> separate whitelisted method)
  /api/service-types  <- src/routes/service-types.ts       (GET list)

Backing docs (Phase-A mapping — nothing is imported here, only read/written):
  Orion lead     <-> Lead      (doc.name = leadCode; custom fields
                                orion_business_line / orion_service_interest /
                                orion_estimated_value / orion_short_name /
                                orion_lead_status; lead_name = clientName;
                                phone = clientPhone)
  Orion client   <-> Customer  (orion_legacy_id; customer_name; disabled=!isActive)
  Orion contract == Project    (KN code = doc.name; the same doc supply_chain.py
                                exposes as a "project" — the two modules agree on
                                every shared field so both screens read alike)
  Orion service_type <-> Orion Service Type (name = code)

Serialization parity: Prisma Decimal columns (estimatedValue, budget) go over the
wire as 2dp JSON *strings* or null (accounting._s2n), matching accounting.py /
supply_chain.py; date-only columns ship as midnight ISO (accounting._iso_date),
timestamps as naive ISO (accounting._iso). ids in payloads resolve via
orion_legacy_id first then ERPNext name (accounting._resolve_name); every response
id is orion_legacy_id when present else doc.name.

Lead contact fields (clientSalutation, clientAddress, source, assignedTo, notes)
round-trip through their orion_* custom fields (orion_client_salutation /
orion_client_address / orion_source / orion_assigned_to / orion_notes — added in
"Phase B: backfill lead/client contact fields"); _lead_create / _lead_update write
them and _lead_read returns them. status reads from orion_lead_status (raw Orion
enum). Still NOT carried: clientId, and _count.projects (always 0 — ERPNext Project
has no lead link, the same gap supply_chain.py notes as `leadId: None`, so a
contract's `lead` is always null too).

Fields the Phase-A schema does NOT carry (emitted null/default, cannot round-trip
without new custom fields):
  - Customer/Client: salutation, phone, email, address, notes have no Customer
           field (migrate/customers.py stored only customer_name + disabled +
           orion_legacy_id), so they read null/"Pak". _count.leads is 0 (no link);
           _count.projects/invoices are derived from the Project/Sales Invoice
           customer link (0 for imported rows, which have no customer set).

POST /api/contracts/analyze carries a file and CANNOT ride the JSON gateway — the
compat @route branch throws with a pointer, and the real endpoint is the
whitelisted `orion.compat.crm.analyze_contract` (multipart, mirrors
bank_statement.upload). feynman's ContractNew.vue analyze() must be re-pointed at
POST /api/method/orion.compat.crm.analyze_contract (see report note).
"""

import json
import re
from datetime import date
from decimal import Decimal

import frappe

from orion.accounting import kn
from orion.compat.accounting import (
    _cc_by_line,
    _insert_named,
    _iso,
    _iso_date,
    _legacy_or_name,
    _money,
    _resolve_name,
    _s2n,
    _settings,
    _wdec,
)
from orion.compat.handle import route, split_path

LEADS_PREFIX = "/api/leads"
CLIENTS_PREFIX = "/api/clients"
CONTRACTS_PREFIX = "/api/contracts"
SERVICE_TYPES_PREFIX = "/api/service-types"

# migrate/leads.py STATUS_MAP: Orion lead status -> stock v16 Lead status.
LEAD_STATUS_MAP = {
    "PROSPECT": "Lead",
    "QUALIFIED": "Interested",
    "PROPOSAL": "Quotation",
    "WON": "Converted",
    "LOST": "Do Not Contact",
}
# supply_chain.py PROJECT_STATUS_TO_LEGACY (kept in sync on purpose).
PROJECT_STATUS_TO_LEGACY = {
    "Open": "ACTIVE",
    "Completed": "COMPLETED",
    "On hold": "ON_HOLD",
    "Cancelled": "CANCELLED",
}

LEAD_FIELDS = [
    "name", "lead_name", "phone", "status", "creation", "modified",
    "orion_business_line", "orion_service_interest", "orion_estimated_value",
    "orion_short_name", "orion_lead_status", "orion_legacy_id",
    "orion_client_salutation", "orion_client_address", "orion_source",
    "orion_assigned_to", "orion_notes",
]
CUSTOMER_FIELDS = [
    "name", "customer_name", "disabled", "orion_legacy_id", "creation", "modified",
    "orion_salutation", "orion_phone", "orion_email", "orion_address", "orion_notes",
]
PROJECT_FIELDS = [
    "name", "project_name", "status", "expected_start_date", "expected_end_date",
    "estimated_costing", "notes", "customer", "cost_center", "creation", "modified",
    "orion_business_line", "kn_sequence", "kontrak_yymm", "lctr",
    "orion_service_type", "target_margin_pct", "orion_program",
    "client_salutation", "client_phone", "client_address", "orion_legacy_id",
]

# Claude model default (bank_statement.py DEFAULT_CLAUDE_MODEL); Orion Settings
# `claude_model` overrides it.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_LCTR_RE = re.compile(r"^[A-Z0-9]{4}$")
_SALUT_RE = re.compile(r"^(Pak|Bu)\s+", re.IGNORECASE)


# ── shared helpers ───────────────────────────────────────────────────────────


def _clean_phone(raw):
    """crm/index.ts cleanPhone: strip separators, +62/62 -> 0. None passes through."""
    if not raw:
        return raw
    p = re.sub(r"[\s\-().]", "", raw)
    if p.startswith("+62"):
        p = "0" + p[3:]
    elif p.startswith("62") and len(p) > 4:
        p = "0" + p[2:]
    return p


def _to_date(value):
    """ISO string / date -> YYYY-MM-DD (or None), like supply_chain._to_date."""
    return value[:10] if value else None


def _parse_budget(value):
    """Budget field -> float of the digit run (source strips non-digits), or None."""
    if value in (None, "", 0):
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return float(digits) if digits else None


def _customers_by_name(names) -> dict:
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


def _ensure_customer(name, client_id=None):
    """Resolve a Customer by orion_legacy_id (clientId) then customer_name, or
    create one (migrate/customers.py conventions). Returns the Customer name."""
    if client_id:
        existing = frappe.db.get_value("Customer", {"orion_legacy_id": client_id})
        if existing:
            return existing
    nm = (name or "").strip()
    if not nm:
        return None
    existing = frappe.db.get_value("Customer", {"customer_name": nm})
    if existing:
        return existing
    from orion.migrate.customers import ensure_customer_group, ensure_territory

    doc = frappe.new_doc("Customer")
    doc.customer_name = nm
    doc.customer_type = "Individual"
    doc.customer_group = ensure_customer_group()
    doc.territory = ensure_territory()
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc.name


def _counts_by_project(doctype: str) -> dict:
    """Row count per Project.project link (v16 rejects SQL funcs as field strings,
    so raw SQL like supply_chain._po_counts). `doctype` is a fixed literal."""
    rows = frappe.db.sql(
        "SELECT project, COUNT(name) AS n FROM `tab%s` "
        "WHERE project IS NOT NULL AND project != '' GROUP BY project" % doctype,
        as_dict=True,
    )
    return {r.project: r.n for r in rows}


def _counts_by_customer(doctype: str) -> dict:
    rows = frappe.db.sql(
        "SELECT customer, COUNT(name) AS n FROM `tab%s` "
        "WHERE customer IS NOT NULL AND customer != '' GROUP BY customer" % doctype,
        as_dict=True,
    )
    return {r.customer: r.n for r in rows}


# ── leads ────────────────────────────────────────────────────────────────────


@route(LEADS_PREFIX)
def leads(path: str, verb: str, payload: dict):
    bare, query = split_path(path)
    rest = bare[len(LEADS_PREFIX):].strip("/")
    parts = [p for p in rest.split("/") if p]
    if verb == "GET" and not parts:
        return _leads_list(query)
    if verb == "POST" and not parts:
        return {"lead": _lead_create(payload)}
    if len(parts) == 1:
        if verb == "PATCH":
            return {"lead": _lead_update(parts[0], payload)}
        if verb == "DELETE":
            return _lead_delete(parts[0])
    frappe.throw("No compat handler for %s %s" % (verb, bare), exc=frappe.DoesNotExistError)


def _lead_row(name: str):
    rows = frappe.get_all("Lead", filters={"name": name}, fields=LEAD_FIELDS)
    return rows[0] if rows else None


def _lead_read(r) -> dict:
    return {
        "id": r.orion_legacy_id or r.name,
        "leadCode": r.name,
        "shortName": r.orion_short_name,
        "clientName": r.lead_name,
        "clientSalutation": r.orion_client_salutation,
        "clientPhone": r.phone,
        "clientAddress": r.orion_client_address,
        "businessLine": r.orion_business_line or "RATUNDA_RENOVASI",
        "serviceInterest": r.orion_service_interest,
        "estimatedValue": _s2n(r.orion_estimated_value),
        "source": r.orion_source,
        "status": r.orion_lead_status or "PROSPECT",
        "assignedTo": r.orion_assigned_to,
        "notes": r.orion_notes,
        "clientId": None,
        "createdAt": _iso(r.creation),
        "updatedAt": _iso(r.modified),
        # ERPNext Project carries no lead link, so no project ever counts here.
        "_count": {"projects": 0},
    }


def _leads_list(query: dict) -> dict:
    """leads GET '?status=&businessLine=' — createdAt desc. status filters on the
    raw Orion status (orion_lead_status), businessLine on orion_business_line."""
    filters = {}
    if query.get("status"):
        filters["orion_lead_status"] = query["status"]
    if query.get("businessLine"):
        filters["orion_business_line"] = query["businessLine"]
    rows = frappe.get_all("Lead", filters=filters, fields=LEAD_FIELDS, order_by="creation desc")
    return {"leads": [_lead_read(r) for r in rows]}


def _next_lead_code() -> str:
    """crm POST leads leadCode generator: LDS-{YYYY}-{NNN}. Max-scan over existing
    Lead codes (doc.name) with the year prefix so it never collides."""
    year = date.today().year
    prefix = "LDS-%d-" % year
    maxseq = 0
    for r in frappe.get_all("Lead", filters={"name": ("like", prefix + "%")}, fields=["name"]):
        segs = r.name.split("-")
        if len(segs) >= 3:
            try:
                maxseq = max(maxseq, int(segs[2]))
            except ValueError:
                pass
    return "%s%03d" % (prefix, maxseq + 1)


def _lead_create(payload: dict) -> dict:
    client_name = payload.get("clientName")
    salutation = payload.get("clientSalutation")
    if not client_name or not salutation:
        frappe.throw("clientName and clientSalutation are required")

    cleaned_phone = _clean_phone(payload.get("clientPhone")) if payload.get("clientPhone") else None
    # Mirror the source's client upsert side-effect (populates the Client/Customer
    # DB) — best-effort, matched by name; never blocks the lead.
    try:
        _ensure_customer(client_name, payload.get("clientId"))
    except Exception:
        pass

    est = payload.get("estimatedValue")
    doc = frappe.new_doc("Lead")
    doc.name = _next_lead_code()
    doc.lead_name = client_name
    doc.phone = cleaned_phone
    doc.status = LEAD_STATUS_MAP["PROSPECT"]
    doc.orion_lead_status = "PROSPECT"
    doc.orion_business_line = payload.get("businessLine") or "RATUNDA_RENOVASI"
    doc.orion_service_interest = payload.get("serviceInterest")
    doc.orion_estimated_value = _money(_wdec(est)) if est not in (None, "") else None
    doc.orion_short_name = (payload.get("shortName") or "").strip() or None
    # Contact fields carried on their orion_* custom fields (crm/index.ts POST parity:
    # source defaults to REFERRAL, the rest pass through null). The read path already
    # returns these — without writing them here the create silently dropped them.
    doc.orion_client_salutation = salutation
    doc.orion_client_address = payload.get("clientAddress")
    doc.orion_source = payload.get("source") or "REFERRAL"
    doc.orion_assigned_to = payload.get("assignedTo")
    doc.orion_notes = payload.get("notes")
    doc.flags.ignore_permissions = True
    _insert_named(doc)
    return _lead_read(_lead_row(doc.name))


def _lead_update(ident: str, payload: dict) -> dict:
    name = _resolve_name("Lead", ident)
    if not name:
        frappe.throw("Lead not found", exc=frappe.DoesNotExistError)
    doc = frappe.get_doc("Lead", name)

    if "shortName" in payload:
        doc.orion_short_name = (payload.get("shortName") or "").strip() or None
    if "clientName" in payload:
        doc.lead_name = payload["clientName"]
    if "clientPhone" in payload:
        cp = payload["clientPhone"]
        doc.phone = _clean_phone(cp) if cp else cp
    if "businessLine" in payload:
        doc.orion_business_line = payload["businessLine"]
    if "serviceInterest" in payload:
        doc.orion_service_interest = payload["serviceInterest"]
    if "estimatedValue" in payload:
        v = payload["estimatedValue"]
        doc.orion_estimated_value = _money(_wdec(v)) if v not in (None, "") else None
    if "status" in payload:
        doc.orion_lead_status = payload["status"]
        doc.status = LEAD_STATUS_MAP.get(payload["status"], doc.status)
    # Contact fields carried on their orion_* custom fields (crm/index.ts PATCH parity:
    # only touch keys the payload actually sends). The read path already returns these.
    if "clientSalutation" in payload:
        doc.orion_client_salutation = payload["clientSalutation"]
    if "clientAddress" in payload:
        doc.orion_client_address = payload["clientAddress"]
    if "source" in payload:
        doc.orion_source = payload["source"]
    if "assignedTo" in payload:
        doc.orion_assigned_to = payload["assignedTo"]
    if "notes" in payload:
        doc.orion_notes = payload["notes"]
    doc.flags.ignore_permissions = True
    doc.save()
    return _lead_read(_lead_row(name))


def _lead_delete(ident: str) -> dict:
    name = _resolve_name("Lead", ident)
    if not name:
        frappe.throw("Lead not found", exc=frappe.DoesNotExistError)
    # Source blocks deleting a lead with linked projects; ERPNext keeps no lead
    # link (count is always 0) so the guard never trips.
    frappe.delete_doc("Lead", name, force=1, ignore_permissions=True)
    return {"ok": True}


# ── clients ──────────────────────────────────────────────────────────────────


@route(CLIENTS_PREFIX)
def clients(path: str, verb: str, payload: dict):
    bare, query = split_path(path)
    rest = bare[len(CLIENTS_PREFIX):].strip("/")
    parts = [p for p in rest.split("/") if p]
    if verb == "GET" and not parts:
        return _clients_list(query)
    if verb == "POST" and not parts:
        return {"client": _client_create(payload)}
    if len(parts) == 1:
        if verb == "GET":
            return {"client": _client_detail(parts[0])}
        if verb == "PATCH":
            return {"client": _client_update(parts[0], payload)}
        if verb == "DELETE":
            return _client_delete(parts[0])
    frappe.throw("No compat handler for %s %s" % (verb, bare), exc=frappe.DoesNotExistError)


def _customer_row(name: str):
    rows = frappe.get_all("Customer", filters={"name": name}, fields=CUSTOMER_FIELDS)
    return rows[0] if rows else None


def _client_read(r) -> dict:
    return {
        "id": r.orion_legacy_id or r.name,
        "name": r.customer_name,
        "salutation": r.orion_salutation or "Pak",
        "phone": r.orion_phone,
        "email": r.orion_email,
        "address": r.orion_address,
        "notes": r.orion_notes,
        "isActive": not r.disabled,
        "createdAt": _iso(r.creation),
        "updatedAt": _iso(r.modified),
    }


def _clients_list(query: dict) -> dict:
    """clients GET '' — name asc, with _count{leads,projects,invoices}. leads=0
    (no Customer<-Lead link); projects/invoices from the Customer link."""
    rows = frappe.get_all("Customer", fields=CUSTOMER_FIELDS, order_by="customer_name asc")
    proj_counts = _counts_by_customer("Project")
    inv_counts = _counts_by_customer("Sales Invoice")
    out = []
    for r in rows:
        c = _client_read(r)
        c["_count"] = {
            "leads": 0,
            "projects": proj_counts.get(r.name, 0),
            "invoices": inv_counts.get(r.name, 0),
        }
        out.append(c)
    return {"clients": out}


def _client_create(payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        frappe.throw("Nama klien wajib diisi")
    # Client names are not unique in the source; Customer is named by
    # customer_name, so suffix duplicates like migrate/customers.py.
    customer_name = name
    n = 1
    while frappe.db.exists("Customer", customer_name):
        n += 1
        customer_name = "%s (%s)" % (name, n)
    from orion.migrate.customers import ensure_customer_group, ensure_territory

    doc = frappe.new_doc("Customer")
    doc.customer_name = customer_name
    doc.customer_type = "Individual"
    doc.customer_group = ensure_customer_group()
    doc.territory = ensure_territory()
    doc.disabled = 0
    doc.flags.ignore_permissions = True
    doc.insert()
    return _client_read(_customer_row(doc.name))


def _si_status_lite(si) -> str:
    if si.docstatus == 2:
        return "CANCELLED"
    if si.docstatus == 0:
        return "DRAFT"
    if _wdec(si.outstanding_amount) <= Decimal("0.01"):
        return "PAID"
    if _wdec(si.grand_total) - _wdec(si.outstanding_amount) > 0:
        return "PARTIAL"
    return "SENT"


def _client_detail(ident: str) -> dict:
    name = _resolve_name("Customer", ident)
    if not name:
        frappe.throw("Not found", exc=frappe.DoesNotExistError)
    client = _client_read(_customer_row(name))
    projects = [
        {
            "id": p.orion_legacy_id or p.name,
            "code": p.name,
            "name": p.project_name,
            "status": PROJECT_STATUS_TO_LEGACY.get(p.status, "ACTIVE"),
        }
        for p in frappe.get_all(
            "Project",
            filters={"customer": name},
            fields=["name", "project_name", "status", "orion_legacy_id"],
            order_by="creation desc",
        )
    ]
    invoices = [
        {
            "id": si.orion_legacy_id or si.name,
            "number": si.name,
            "status": _si_status_lite(si),
            "totalAmount": _s2n(si.grand_total),
        }
        for si in frappe.get_all(
            "Sales Invoice",
            filters={"customer": name},
            fields=["name", "orion_legacy_id", "docstatus", "grand_total", "outstanding_amount"],
            order_by="creation desc",
        )
    ]
    client["leads"] = []  # no Customer<-Lead link in ERPNext
    client["projects"] = projects
    client["invoices"] = invoices
    return client


def _client_update(ident: str, payload: dict) -> dict:
    name = _resolve_name("Customer", ident)
    if not name:
        frappe.throw("Not found", exc=frappe.DoesNotExistError)
    doc = frappe.get_doc("Customer", name)
    if "name" in payload:
        new = (payload["name"] or "").strip()
        if new:
            doc.customer_name = new
    if "isActive" in payload:
        doc.disabled = 0 if payload["isActive"] else 1
    # salutation / phone / email / address / notes: no Customer field.
    doc.flags.ignore_permissions = True
    doc.save()
    return _client_read(_customer_row(name))


def _client_delete(ident: str) -> dict:
    name = _resolve_name("Customer", ident)
    if not name:
        frappe.throw("Not found", exc=frappe.DoesNotExistError)
    linked = frappe.db.count("Project", {"customer": name}) + frappe.db.count(
        "Sales Invoice", {"customer": name}
    )
    if linked:
        frappe.throw("Klien masih memiliki data terkait (leads/proyek/invoice)")
    frappe.delete_doc("Customer", name, force=1, ignore_permissions=True)
    return {"success": True}


# ── contracts (== Projects with a KN sequence) ───────────────────────────────


@route(CONTRACTS_PREFIX)
def contracts(path: str, verb: str, payload: dict):
    bare, query = split_path(path)
    rest = bare[len(CONTRACTS_PREFIX):].strip("/")
    parts = [p for p in rest.split("/") if p]
    if verb == "GET" and not parts:
        return _contracts_list(query)
    if verb == "POST" and not parts:
        return {"project": _contract_create(payload)}
    if verb == "GET" and parts == ["kn-next"]:
        return _kn_next(query)
    if verb == "GET" and parts == ["lctr-availability"]:
        return _lctr_availability(query)
    if verb == "POST" and parts == ["analyze"]:
        frappe.throw(
            "POST %s/analyze carries a file and cannot go through the JSON gateway "
            "— POST multipart to /api/method/orion.compat.crm.analyze_contract instead"
            % CONTRACTS_PREFIX
        )
    if len(parts) == 1 and verb == "GET":
        return {"contract": _contract_detail(parts[0])}
    if len(parts) == 1 and verb == "PATCH":
        return {"contract": _contract_update(parts[0], payload)}
    frappe.throw("No compat handler for %s %s" % (verb, bare), exc=frappe.DoesNotExistError)


def _project_row(ident: str):
    name = _resolve_name("Project", ident)
    if not name:
        return None
    rows = frappe.get_all("Project", filters={"name": name}, fields=PROJECT_FIELDS)
    return rows[0] if rows else None


def _contract_read(r, customers: dict, counts: dict | None = None) -> dict:
    customer = customers.get(r.customer) if r.customer else None
    out = {
        "id": r.orion_legacy_id or r.name,
        "code": r.name,
        "name": r.project_name,
        # clientName lives on the Customer link (unset for imported rows, like
        # supply_chain.py); clientSalutation/phone/address on the Project fields.
        "clientName": customer.customer_name if customer else None,
        "clientSalutation": r.client_salutation or "Pak",
        "clientPhone": r.client_phone,
        "address": r.client_address,
        "businessLine": r.orion_business_line or "RATUNDA_RENOVASI",
        "status": PROJECT_STATUS_TO_LEGACY.get(r.status, "ACTIVE"),
        "startDate": _iso_date(r.expected_start_date),
        "endDate": _iso_date(r.expected_end_date),
        "budget": _s2n(r.estimated_costing),
        "targetMarginPct": r.target_margin_pct,
        "description": r.notes,
        "lctr": r.lctr,
        "knSequence": r.kn_sequence,
        "kontrakYYMM": r.kontrak_yymm,
        "serviceTypeCode": r.orion_service_type,
        "leadId": None,  # lead linkage not carried into ERPNext Project
        "clientId": (customer.orion_legacy_id or customer.name) if customer else None,
        "programId": _legacy_or_name("Orion Program", r.orion_program) if r.orion_program else None,
        "kontrakDraftFile": None,  # draft-file binaries not migrated
        "createdAt": _iso(r.creation),
        "updatedAt": _iso(r.modified),
        "lead": None,  # ERPNext Project has no lead link
    }
    if counts is not None:
        out["_count"] = counts
    return out


def _contracts_list(query: dict) -> dict:
    """contracts GET '?businessLine=' — only Projects with a KN sequence, ordered
    kontrakYYMM desc, knSequence desc; each with _count{invoices,disbursements,
    purchaseOrders} and a lead stub (null — no ERPNext lead link)."""
    filters = {"kn_sequence": ("is", "set")}
    if query.get("businessLine"):
        filters["orion_business_line"] = query["businessLine"]
    rows = frappe.get_all(
        "Project",
        filters=filters,
        fields=PROJECT_FIELDS,
        order_by="kontrak_yymm desc, kn_sequence desc",
    )
    customers = _customers_by_name({r.customer for r in rows if r.customer})
    inv_counts = _counts_by_project("Sales Invoice")
    disb_counts = _counts_by_project("Orion Cash Advance")
    po_counts = _counts_by_project("Purchase Order")
    out = [
        _contract_read(
            r,
            customers,
            counts={
                "invoices": inv_counts.get(r.name, 0),
                "disbursements": disb_counts.get(r.name, 0),
                "purchaseOrders": po_counts.get(r.name, 0),
            },
        )
        for r in rows
    ]
    return {"contracts": out}


def _contract_detail(ident: str) -> dict:
    r = _project_row(ident)
    if not r or r.kn_sequence is None:
        frappe.throw("Contract not found", exc=frappe.DoesNotExistError)
    customers = _customers_by_name({r.customer} if r.customer else set())
    return _contract_read(r, customers)


def _contract_create(payload: dict) -> dict:
    name = payload.get("name")
    client_name = payload.get("clientName")
    lctr = payload.get("lctr")
    kn_seq = payload.get("knSequence")
    yymm = payload.get("kontrakYYMM")
    svc = payload.get("serviceTypeCode")
    if not (name and client_name and lctr and kn_seq and yymm and svc):
        frappe.throw(
            "name, clientName, lctr, knSequence, kontrakYYMM, serviceTypeCode are required"
        )
    if not _LCTR_RE.match(str(lctr)):
        frappe.throw("lctr must be exactly 4 uppercase alphanumeric characters")

    business_line = payload.get("businessLine") or "RATUNDA_RENOVASI"
    year_prefix = str(yymm)[:2]
    conflict = frappe.get_all(
        "Project",
        filters={
            "lctr": lctr,
            "orion_business_line": business_line,
            "kn_sequence": ("is", "set"),
            "kontrak_yymm": ("like", year_prefix + "%"),
        },
        fields=["name"],
        limit=1,
    )
    if conflict:
        frappe.throw("LCTR '%s' already used in %s by %s" % (lctr, year_prefix, conflict[0].name))

    salutation = (payload.get("clientSalutation") or "Pak").strip() or "Pak"
    raw = str(client_name).strip()
    plain = _SALUT_RE.sub("", raw).strip() or raw
    customer = _ensure_customer(plain, payload.get("clientId"))

    effective = "POIESIS_STUDIO" if business_line == "POIESIS_STUDIO" else "RATUNDA_RENOVASI"
    if kn_seq in (None, ""):
        kn_seq = kn.next_kn_sequence(effective, year_prefix)
    code = kn.build_code(business_line, int(kn_seq), yymm[:2], yymm[2:], lctr, svc)

    settings = _settings()
    doc = frappe.new_doc("Project")
    doc.name = code
    doc.project_name = name
    doc.status = "Open"
    doc.company = settings.company
    doc.cost_center = _cc_by_line().get(business_line) or settings.shared_cost_center
    doc.customer = customer
    doc.client_salutation = salutation
    doc.client_phone = payload.get("clientPhone")
    doc.client_address = payload.get("address")
    doc.orion_business_line = business_line
    doc.expected_start_date = _to_date(payload.get("startDate"))
    doc.expected_end_date = _to_date(payload.get("endDate"))
    doc.estimated_costing = _parse_budget(payload.get("budget"))
    doc.notes = payload.get("description")
    doc.lctr = lctr
    doc.kn_sequence = int(kn_seq)
    doc.kontrak_yymm = yymm
    doc.orion_service_type = svc
    # leadId / kontrakDraftFile: no ERPNext Project field carries them.
    doc.flags.ignore_permissions = True
    _insert_named(doc)
    customers = _customers_by_name({customer} if customer else set())
    return _contract_read(_project_row(code), customers)


def _contract_update(ident: str, payload: dict) -> dict:
    name = _resolve_name("Project", ident)
    if not name:
        frappe.throw("Contract not found", exc=frappe.DoesNotExistError)
    doc = frappe.get_doc("Project", name)

    if "name" in payload:
        doc.project_name = payload["name"]
    if "clientName" in payload:
        raw = str(payload["clientName"] or "").strip()
        plain = _SALUT_RE.sub("", raw).strip() or raw
        if plain:
            doc.customer = _ensure_customer(plain) or doc.customer
    if "clientSalutation" in payload:
        doc.client_salutation = payload["clientSalutation"]
    if "clientPhone" in payload:
        doc.client_phone = payload["clientPhone"] or None
    if "address" in payload:
        doc.client_address = payload["address"] or None
    if "budget" in payload:
        doc.estimated_costing = _parse_budget(payload["budget"])
    if "description" in payload:
        doc.notes = payload["description"] or None
    if "serviceTypeCode" in payload:
        doc.orion_service_type = payload["serviceTypeCode"]
    if "startDate" in payload:
        doc.expected_start_date = _to_date(payload["startDate"])
    if "endDate" in payload:
        doc.expected_end_date = _to_date(payload["endDate"])
    # leadId: no ERPNext Project field — accepted but not persisted.
    doc.flags.ignore_permissions = True
    doc.save()
    customers = _customers_by_name({doc.customer} if doc.customer else set())
    return _contract_read(_project_row(doc.name), customers)


def _kn_next(query: dict) -> dict:
    """kn-next?businessLine= — next per-line/per-year KN running number for the
    CURRENT year (kn.next_kn_sequence = MAX(kn_sequence)+1 over that scope)."""
    bl = "POIESIS_STUDIO" if query.get("businessLine") == "POIESIS_STUDIO" else "RATUNDA_RENOVASI"
    year = date.today().year
    return {
        "next": kn.next_kn_sequence(bl, year),
        "year": "%02d" % (year % 100),
        "businessLine": bl,
    }


def _lctr_availability(query: dict) -> dict:
    """lctr-availability?lctr=&kontrakYYMM=&businessLine= — the same rule POST
    enforces. A same-line hit is a conflict; an opposite-line hit is the intended
    design<->build pairing (surfaced as pairsWith, only when there is no conflict)."""
    lctr = str(query.get("lctr") or "").upper()
    yymm = str(query.get("kontrakYYMM") or "")
    bl = "POIESIS_STUDIO" if query.get("businessLine") == "POIESIS_STUDIO" else "RATUNDA_RENOVASI"
    if not _LCTR_RE.match(lctr):
        frappe.throw("lctr must be exactly 4 uppercase alphanumeric characters")
    year_prefix = yymm[:2] if yymm else "%02d" % (date.today().year % 100)

    def find(business_line):
        rows = frappe.get_all(
            "Project",
            filters={
                "lctr": lctr,
                "orion_business_line": business_line,
                "kn_sequence": ("is", "set"),
                "kontrak_yymm": ("like", year_prefix + "%"),
            },
            fields=["name", "project_name"],
            limit=1,
        )
        return {"code": rows[0].name, "name": rows[0].project_name} if rows else None

    conflict = find(bl)
    pairing = None
    if not conflict:
        pairing = find("RATUNDA_RENOVASI" if bl == "POIESIS_STUDIO" else "POIESIS_STUDIO")
    return {
        "lctr": lctr,
        "year": year_prefix,
        "businessLine": bl,
        "available": not conflict,
        "conflict": conflict,
        "pairsWith": pairing,
    }


# ── service types ────────────────────────────────────────────────────────────


@route(SERVICE_TYPES_PREFIX)
def service_types(path: str, verb: str, payload: dict):
    bare, query = split_path(path)
    rest = bare[len(SERVICE_TYPES_PREFIX):].strip("/")
    if verb == "GET" and not rest:
        return _service_types_list(query)
    # service-types.ts exposes GET only.
    frappe.throw("No compat handler for %s %s" % (verb, bare), exc=frappe.DoesNotExistError)


def _service_types_list(query: dict) -> dict:
    """service-types GET '?businessLine=' — active only, businessLine in {bl, ALL},
    ordered businessLine asc, code asc."""
    filters = {"is_active": 1}
    bl = query.get("businessLine")
    if bl:
        filters["business_line"] = ("in", [bl, "ALL"])
    rows = frappe.get_all(
        "Orion Service Type",
        filters=filters,
        fields=["name", "code", "label", "business_line", "is_active", "creation", "modified"],
        order_by="business_line asc, code asc",
    )
    return {
        "serviceTypes": [
            {
                "id": r.name,
                "code": r.code,
                "label": r.label,
                "businessLine": r.business_line,
                "isActive": bool(r.is_active),
                "createdAt": _iso(r.creation),
                "updatedAt": _iso(r.modified),
            }
            for r in rows
        ]
    }


# ── contract analyze (multipart + Claude — off the JSON gateway) ─────────────


_ANALYZE_PROMPT = """Berikut adalah dokumen kontrak/perjanjian (terlampir).

Analisis dokumen ini dan kembalikan JSON dengan struktur PERSIS berikut (tidak ada teks lain):
{
  "clientName": "nama klien / pemilik proyek (string)",
  "clientPhone": "nomor telepon klien jika ada (string atau null)",
  "projectLocation": "alamat / lokasi proyek (string)",
  "projectDescription": "deskripsi singkat pekerjaan / scope of work (string, maks 120 karakter)",
  "projectValue": angka_numerik_nilai_kontrak_dalam_IDR_atau_null,
  "paymentTerms": "ringkasan termin pembayaran (string)",
  "lctrSuggestions": ["KODE1", "KODE2", "KODE3"]
}

Untuk lctrSuggestions: buat 3 kode singkat 4 karakter HURUF KAPITAL yang merepresentasikan nama klien atau proyek.
Gunakan singkatan nama klien (misal: "Budi Santoso" -> "BDST", "Amita Rahayu" -> "AMTR"),
atau singkatan lokasi (misal: "Kebayoran" -> "KBYR", "Pondok Indah" -> "PDIN").
Ketiga kode harus berbeda dan 4 karakter tepat.

Kembalikan HANYA JSON yang valid, tanpa markdown, tanpa penjelasan tambahan."""

_ANALYZE_FALLBACK = {
    "docId": None,
    "clientName": "",
    "clientPhone": None,
    "projectLocation": "",
    "projectDescription": "",
    "projectValue": None,
    "paymentTerms": "",
    "lctrSuggestions": [],
}


@frappe.whitelist(methods=["POST"])
def analyze_contract():
    """Multipart contract-analysis endpoint (replaces POST /api/contracts/analyze,
    which cannot ride the JSON gateway). Accepts form field `file` (a PDF) and
    returns the source-shaped analysis JSON inside Frappe's {"message": ...}
    envelope. On any AI/config failure it returns a source-shaped "manual entry"
    fallback (empty fields) rather than a 500 — the wizard still lets the user
    type the details and the LCTR by hand.

    Unlike the source (which routes the doc through Otto for text extraction),
    this sends the PDF straight to Claude as a document block (the
    bank_statement.py path). Non-PDF uploads (DOCX) fall back to manual entry.
    """
    if frappe.session.user in ("Guest", "", None):
        raise frappe.AuthenticationError

    file = frappe.request.files.get("file")
    if file is None or not file.filename:
        frappe.throw("No file provided")

    api_key = frappe.conf.get("anthropic_api_key")
    is_pdf = (file.mimetype or "").lower() == "application/pdf" or file.filename.lower().endswith(
        ".pdf"
    )
    if not api_key or not is_pdf:
        # No key configured, or a DOCX we can't extract without Otto -> manual entry.
        return dict(_ANALYZE_FALLBACK)

    try:
        import base64

        import anthropic

        model = frappe.get_cached_doc("Orion Settings").claude_model or DEFAULT_CLAUDE_MODEL
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": base64.b64encode(file.stream.read()).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": _ANALYZE_PROMPT},
                    ],
                }
            ],
        )
        first = msg.content[0] if msg.content else None
        raw = first.text.strip() if getattr(first, "type", None) == "text" else ""
        parsed = json.loads(_FENCE.sub("", raw.strip()))
    except Exception:
        # AI unavailable / bad JSON — never a 500; let the user enter details.
        return dict(_ANALYZE_FALLBACK)

    value = parsed.get("projectValue")
    suggestions = parsed.get("lctrSuggestions")
    return {
        "docId": None,
        "clientName": parsed.get("clientName") or "",
        "clientPhone": parsed.get("clientPhone"),
        "projectLocation": parsed.get("projectLocation") or "",
        "projectDescription": parsed.get("projectDescription") or "",
        "projectValue": value if isinstance(value, (int, float)) else None,
        "paymentTerms": parsed.get("paymentTerms") or "",
        "lctrSuggestions": suggestions[:3] if isinstance(suggestions, list) else [],
    }
