"""Payment-reconciliation + IT compat handlers.

Mirrors two bellatrix router groups so the matching feynman screens work
unchanged against ERPNext data:

  /api/accounting/payment-reconciliation
        <- bellatrix-python app/routers/accounting/payment_reconciliation.py
           (GET '', GET /plan, POST /apply, POST /apply-fifo, POST /run) driven
           by app/services/accounting/advance_reconciliation.py
  /api/it/users          <- bellatrix (Node) src/routes/it/index.ts GET /users
  /api/it/subscriptions  <- bellatrix (Node) src/routes/it/index.ts GET /subscriptions

── Advance reconciliation model (source vs ERPNext) ──────────────────────────
In the source, client cash imported via the bank-statement flow is booked as a
journal line CREDITING a client-advance liability account (2-1210 Ratunda /
2-1220 Poiesis) and never creates an InvoicePayment, so it never advances an
invoice. The "unapplied advance" per project = Σ(credit − debit) over those
accounts (advance_reconciliation.py:45-71).

In this ERPNext migration the same cash lands on 2-1210/2-1220 as a Payment
Entry (payment_type Receive) or a bank-import Journal Entry — either way the GL
Entry on the advance account is a CREDIT. So the read side computes the net
advance from `tabGL Entry` on those accounts (voucher-type agnostic), exactly
mirroring the source's line-level Σ(credit − debit). Open invoices are submitted
Sales Invoices with outstanding_amount > Rp 1 (the SENT/PARTIAL/OVERDUE set).

── Apply mechanism (verified) ────────────────────────────────────────────────
"Applying" an advance to an invoice posts a GL-neutral reclass Journal Entry —
Dr advance liability (2-121x) / Cr the invoice's AR account (its debit_to,
1-121x) — with the AR credit row carrying party_type=Customer, party=<customer>,
reference_type="Sales Invoice", reference_name=<SI>. On submit ERPNext allocates
that credit against the invoice and reduces its outstanding_amount. This is the
exact mechanism the migration itself uses for JE-based invoice payments
(orion.migrate.invoices.rebuild_reclass, which sets those same four fields on the
debit_to credit row; and orion.compat.accounting_writes._inv_payments, which
reconstructs invoice payments from precisely such JEs). Net of the original
bank-import (Dr Bank / Cr Advance) and this reclass is Dr Bank / Cr AR — a normal
payment, no double count. Applying debits the advance account, so the project's
net advance drops to ~0 and a re-run is idempotent (advance_reconciliation.py
docstring + apply_eligible:163-247).

Money is emitted as JSON numbers (float), matching the source router's `_f =
float` and feynman's parseFloat consumers (not the 2dp-string convention the
accounting.py doc-value fields use). Rupiah tolerance is exactly Rp 1
(advance_reconciliation.TOLERANCE).
"""

from collections import defaultdict
from decimal import Decimal

import frappe

from orion.compat.accounting import (
    _account_by_number,
    _cc_by_line,
    _company,
    _iso,
    _legacy_or_name,
    _money,
    _resolve_name,
    _settings,
    _wdec,
)
from orion.compat.auth import orion_role
from orion.compat.handle import route, split_path

PR_PREFIX = "/api/accounting/payment-reconciliation"

# Client receivable (Piutang Usaha) — debited on invoice send, credited on payment.
AR_CODES = ("1-1210", "1-1220")
# Client advance / DP (liability) — credited when client cash lands in the GL.
ADVANCE_CODES = ("2-1210", "2-1220")

# Rupiah are whole-number; allow a 1-rupiah slack for rounding noise
# (advance_reconciliation.TOLERANCE).
TOLERANCE = Decimal("1.00")
_ZERO = Decimal("0")


# ── account resolution ───────────────────────────────────────────────────────


def _account_maps() -> tuple[dict, dict]:
    """({advance account name -> code}, {AR account name -> code}) for the
    company's leaf 2-121x / 1-121x accounts."""
    adv: dict[str, str] = {}
    ar: dict[str, str] = {}
    for code in ADVANCE_CODES:
        row = _account_by_number(code)
        if row:
            adv[row.name] = code
    for code in AR_CODES:
        row = _account_by_number(code)
        if row:
            ar[row.name] = code
    return adv, ar


def _bl_from_debit_to(debit_to: str) -> str:
    """Business line from the SI's receivable account (1-1220 -> Poiesis)."""
    num = frappe.db.get_value("Account", debit_to, "account_number") if debit_to else None
    return "POIESIS_STUDIO" if num == "1-1220" else "RATUNDA_RENOVASI"


def _diso(d) -> str | None:
    """A date in the source's `date.isoformat()` shape ('2026-01-02')."""
    if not d:
        return None
    return frappe.utils.getdate(d).isoformat()


# ── GL aggregation ───────────────────────────────────────────────────────────


def _advance_gl_rows(company: str, adv_names: list) -> list:
    if not adv_names:
        return []
    return frappe.get_all(
        "GL Entry",
        filters={"company": company, "is_cancelled": 0, "account": ("in", adv_names)},
        fields=[
            "name", "posting_date", "account", "voucher_type", "voucher_no",
            "remarks", "project", "party", "debit", "credit",
        ],
    )


def _net_advance_by_project(company: str, adv: dict) -> dict:
    """{project_name: {code: net_credit>0}} for project-tagged advances —
    advance_reconciliation._net_advance_by_project (45-71). net = Σ(credit-debit);
    zero/negative residue per account is dropped, as are projects with no positive
    net."""
    out: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: _ZERO))
    for g in _advance_gl_rows(company, list(adv)):
        pid = g.project or None
        if not pid:
            continue
        code = adv[g.account]
        out[pid][code] += _wdec(g.credit) - _wdec(g.debit)
    cleaned: dict[str, dict[str, Decimal]] = {}
    for pid, by_code in out.items():
        pos = {c: v for c, v in by_code.items() if v > 0}
        if pos:
            cleaned[pid] = pos
    return cleaned


def _open_invoices_by_project(company: str, pids: list) -> tuple[dict, dict]:
    """(open invoices+remaining per project [oldest-first], ALL invoices per
    project) — advance_reconciliation._open_invoices_by_project (74-91). Open =
    submitted (docstatus 1) with outstanding_amount > Rp 1; ALL includes drafts
    so the worklist can tell ONLY_DRAFT from NO_INVOICE."""
    open_by: dict[str, list] = defaultdict(list)
    all_by: dict[str, list] = defaultdict(list)
    if not pids:
        return open_by, all_by
    rows = frappe.get_all(
        "Sales Invoice",
        filters={"company": company, "docstatus": ("in", (0, 1)), "project": ("in", pids)},
        fields=[
            "name", "orion_legacy_id", "customer", "posting_date", "project",
            "grand_total", "outstanding_amount", "debit_to", "docstatus",
        ],
        order_by="posting_date asc, creation asc",
    )
    for si in rows:
        all_by[si.project].append(si)
        rem = _wdec(si.outstanding_amount)
        if si.docstatus == 1 and rem > TOLERANCE:
            open_by[si.project].append((si, rem))
    return open_by, all_by


# ── plan (dry run) ───────────────────────────────────────────────────────────


def _build_plan(company: str) -> dict:
    """Classify each project's unapplied advance into eligible / queue / worklist
    — advance_reconciliation.build_plan (94-160). Eligible/queue rows carry
    internal `_`-prefixed handles (project/SI names, customer, debit_to) used by
    apply; _reconciliation_plan strips them before returning."""
    adv, _ar = _account_maps()
    net = _net_advance_by_project(company, adv)
    pids = list(net.keys())
    if not pids:
        return {"eligible": [], "queue": [], "worklist": []}

    open_by, all_by = _open_invoices_by_project(company, pids)
    eligible: list[dict] = []
    queue: list[dict] = []
    worklist: list[dict] = []

    for pid in pids:
        by_code = net[pid]
        total_net = sum(by_code.values(), _ZERO)
        advances = {c: float(v) for c, v in by_code.items()}
        open_invs = open_by.get(pid, [])

        if not open_invs:
            subtype = "ONLY_DRAFT" if all_by.get(pid) else "NO_INVOICE"
            worklist.append(
                {"projectId": _legacy_or_name("Project", pid), "amount": float(total_net),
                 "advances": advances, "subtype": subtype, "_pid": pid}
            )
            continue

        # Exact match: a single open invoice whose remaining equals the net advance.
        exact = [(si, r) for (si, r) in open_invs if abs(r - total_net) <= TOLERANCE]
        chosen = None
        if len(open_invs) == 1 and abs(open_invs[0][1] - total_net) <= TOLERANCE:
            chosen = open_invs[0]
        elif len(exact) == 1:
            chosen = exact[0]

        if chosen is not None:
            si, rem = chosen
            eligible.append(
                {
                    "projectId": _legacy_or_name("Project", pid),
                    "invoiceId": _legacy_or_name("Sales Invoice", si.name),
                    "invoiceNumber": si.name,
                    "businessLine": _bl_from_debit_to(si.debit_to),
                    "amount": float(total_net),
                    "invoiceRemaining": float(rem),
                    "advances": advances,
                    "_pid": pid,
                    "_si": si.name,
                    "_customer": si.customer,
                    "_debit_to": si.debit_to,
                }
            )
        else:
            queue.append(
                {
                    "projectId": _legacy_or_name("Project", pid),
                    "amount": float(total_net),
                    "advances": advances,
                    "openInvoices": [
                        {"id": _legacy_or_name("Sales Invoice", si.name), "number": si.name,
                         "remaining": float(r)}
                        for (si, r) in open_invs
                    ],
                    "reason": "no single open invoice matches the advance amount",
                    "_pid": pid,
                }
            )

    return {"eligible": eligible, "queue": queue, "worklist": worklist}


def _project_meta(pids: set) -> dict:
    """{project_name: {code, name, clientName, businessLine}} — code is the
    ERPNext Project name, clientName the linked Customer's name (supply_chain
    _project_read: code=name, name=project_name)."""
    out: dict[str, dict] = {}
    if not pids:
        return out
    rows = frappe.get_all(
        "Project",
        filters={"name": ("in", list(pids))},
        fields=["name", "project_name", "customer", "orion_business_line"],
    )
    cust_names: dict[str, str] = {}
    custs = {r.customer for r in rows if r.customer}
    if custs:
        for c in frappe.get_all(
            "Customer", filters={"name": ("in", list(custs))}, fields=["name", "customer_name"]
        ):
            cust_names[c.name] = c.customer_name
    for r in rows:
        out[r.name] = {
            "code": r.name,
            "name": r.project_name,
            "clientName": cust_names.get(r.customer),
            "businessLine": r.orion_business_line,
        }
    return out


def _enrich(items: list, meta: dict) -> list:
    """payment_reconciliation._enrich (160-167): add code/name/clientName."""
    for it in items:
        m = meta.get(it.get("_pid"), {})
        it["code"] = m.get("code")
        it["name"] = m.get("name") or "(proyek tidak dikenal)"
        it["clientName"] = m.get("clientName")
    return items


def _strip_internal(items: list) -> None:
    for it in items:
        for k in [k for k in it if k.startswith("_")]:
            del it[k]


def _reconciliation_plan() -> dict:
    """GET /plan — payment_reconciliation.reconciliation_plan (170-191)."""
    company = _company()
    plan = _build_plan(company)
    pids = {
        it["_pid"]
        for key in ("eligible", "queue", "worklist")
        for it in plan[key]
        if it.get("_pid")
    }
    meta = _project_meta(pids)
    for key in ("eligible", "queue", "worklist"):
        _enrich(plan[key], meta)
    plan["totals"] = {
        "eligible": len(plan["eligible"]),
        "eligibleValue": sum((e["amount"] for e in plan["eligible"]), 0.0),
        "queue": len(plan["queue"]),
        "worklist": len(plan["worklist"]),
        "worklistValue": sum((w["amount"] for w in plan["worklist"]), 0.0),
    }
    for key in ("eligible", "queue", "worklist"):
        _strip_internal(plan[key])
    return plan


# ── overview (read-only report) ──────────────────────────────────────────────


def _overview() -> dict:
    """GET '' — payment_reconciliation.payment_reconciliation (53-157). Feynman
    consumes `totals` and `unattributed`; `accounts`/`projects` are emitted for
    source parity."""
    company = _company()
    adv, ar = _account_maps()

    # Advance-account GL: per-project net + untagged (project-less) lines.
    adv_net_by_proj: dict = defaultdict(lambda: _ZERO)  # includes the None key
    untagged: list = []  # (row, code, net)
    for g in _advance_gl_rows(company, list(adv)):
        code = adv[g.account]
        net = _wdec(g.credit) - _wdec(g.debit)
        pid = g.project or None
        adv_net_by_proj[pid] += net
        if pid is None and net != 0:
            untagged.append((g, code, net))

    untagged_entries = _untagged_entries(untagged)

    # AR-account GL credits (info / sanity vs invoicePayments).
    ar_credit: dict = defaultdict(lambda: _ZERO)
    if ar:
        for g in frappe.get_all(
            "GL Entry",
            filters={"company": company, "is_cancelled": 0, "account": ("in", list(ar))},
            fields=["project", "credit"],
        ):
            ar_credit[g.project or None] += _wdec(g.credit)

    # Invoice-side collected: paid = grand_total − outstanding over submitted SIs.
    invoice_paid: dict = defaultdict(lambda: _ZERO)
    for si in frappe.get_all(
        "Sales Invoice",
        filters={"company": company, "docstatus": 1},
        fields=["project", "grand_total", "outstanding_amount"],
    ):
        invoice_paid[si.project or None] += _wdec(si.grand_total) - _wdec(si.outstanding_amount)

    pids = (set(invoice_paid) | set(adv_net_by_proj) | set(ar_credit)) - {None}
    meta = _project_meta(pids)
    rows = []
    for pid in pids:
        m = meta.get(pid, {})
        adv_v = adv_net_by_proj.get(pid, _ZERO)
        rows.append(
            {
                "id": _legacy_or_name("Project", pid),
                "code": m.get("code") or pid,
                "name": m.get("name") or "(proyek tidak dikenal)",
                "clientName": m.get("clientName"),
                "businessLine": m.get("businessLine"),
                "invoicePayments": float(invoice_paid.get(pid, _ZERO)),
                "glArCredits": float(ar_credit.get(pid, _ZERO)),
                "unappliedAdvance": float(adv_v),
                "deviation": float(adv_v),
            }
        )
    rows.sort(key=lambda r: abs(r["deviation"]), reverse=True)

    tagged_adv = sum((v for k, v in adv_net_by_proj.items() if k is not None), _ZERO)
    untagged_adv = adv_net_by_proj.get(None, _ZERO)

    return {
        "accounts": {"ar": list(AR_CODES), "advance": list(ADVANCE_CODES)},
        "projects": rows,
        "unattributed": {"total": float(untagged_adv), "entries": untagged_entries},
        "totals": {
            "invoicePayments": float(sum(invoice_paid.values(), _ZERO)),
            "unappliedAdvanceTagged": float(tagged_adv),
            "unappliedAdvanceUntagged": float(untagged_adv),
            "totalGap": float(tagged_adv + untagged_adv),
        },
    }


def _untagged_entries(untagged: list) -> list:
    """Per-line untagged advance entries {journalEntryId, date, description,
    reference, accountCode, amount}, sorted by |amount| desc. description/
    reference are enriched from the backing JE (user_remark/cheque_no) or Payment
    Entry (remarks/reference_no)."""
    je_names = list({g.voucher_no for (g, _c, _n) in untagged if g.voucher_type == "Journal Entry"})
    pe_names = list({g.voucher_no for (g, _c, _n) in untagged if g.voucher_type == "Payment Entry"})
    je_meta = {}
    if je_names:
        je_meta = {
            r.name: r
            for r in frappe.get_all(
                "Journal Entry",
                filters={"name": ("in", je_names)},
                fields=["name", "user_remark", "cheque_no", "orion_legacy_id"],
            )
        }
    pe_meta = {}
    if pe_names:
        pe_meta = {
            r.name: r
            for r in frappe.get_all(
                "Payment Entry",
                filters={"name": ("in", pe_names)},
                fields=["name", "remarks", "reference_no", "orion_legacy_id"],
            )
        }

    entries = []
    for g, code, net in untagged:
        vt, vn = g.voucher_type, g.voucher_no
        je_id, desc, ref = vn, None, None
        if vt == "Journal Entry" and vn in je_meta:
            m = je_meta[vn]
            je_id = m.orion_legacy_id or vn
            desc = m.user_remark
            ref = m.cheque_no
        elif vt == "Payment Entry" and vn in pe_meta:
            m = pe_meta[vn]
            je_id = m.orion_legacy_id or vn
            desc = m.remarks
            ref = m.reference_no if (m.reference_no and m.reference_no != "-") else None
        if not desc:
            desc = (g.remarks if g.remarks and g.remarks != "No Remarks" else None) or vn
        entries.append(
            {
                "journalEntryId": je_id,
                "date": _diso(g.posting_date),
                "description": desc,
                "reference": ref,
                "accountCode": code,
                "amount": float(net),
            }
        )
    entries.sort(key=lambda e: abs(e["amount"]), reverse=True)
    return entries


# ── apply (reclass advance -> AR against the invoice) ────────────────────────


def _post_reclass_je(*, si_name, debit_to, customer, advance_account, amount,
                     business_line, posting_date, reference, code):
    """Dr advance liability / Cr AR (debit_to) referencing the Sales Invoice.
    Mirrors orion.migrate.invoices.rebuild_reclass (543-549): the AR credit row
    carries party + reference_type/reference_name so ERPNext allocates it against
    the SI on submit (reducing outstanding_amount). GL-neutral reclass."""
    settings = _settings()
    cc = _cc_by_line().get(business_line) or settings.shared_cost_center

    doc = frappe.new_doc("Journal Entry")
    doc.voucher_type = "Journal Entry"
    doc.company = settings.company
    doc.posting_date = posting_date
    doc.user_remark = "Aplikasi uang muka klien ke %s" % si_name
    doc.multi_currency = 0
    doc.orion_source_type = "AR_INVOICE"
    if reference:
        doc.cheque_no = reference
        doc.cheque_date = posting_date
    doc.append(
        "accounts",
        {
            "account": advance_account,
            "debit_in_account_currency": _money(amount),
            "credit_in_account_currency": 0,
            "cost_center": cc,
            "user_remark": "Pakai uang muka klien (%s) — %s" % (code, si_name),
        },
    )
    doc.append(
        "accounts",
        {
            "account": debit_to,
            "debit_in_account_currency": 0,
            "credit_in_account_currency": _money(amount),
            "cost_center": cc,
            "party_type": "Customer",
            "party": customer,
            "reference_type": "Sales Invoice",
            "reference_name": si_name,
            "user_remark": "Aplikasi uang muka klien ke %s" % si_name,
        },
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    doc.submit()
    return doc


def _si_status_after(si_name: str) -> str:
    """Orion invoice status from live ERPNext SI totals (mirrors
    accounting_writes._si_status for a submitted invoice)."""
    grand, outstanding = frappe.db.get_value(
        "Sales Invoice", si_name, ["grand_total", "outstanding_amount"]
    )
    o = _wdec(outstanding)
    if o <= Decimal("0.01"):
        return "PAID"
    if _wdec(grand) - o > 0:
        return "PARTIAL"
    return "SENT"


def _apply_eligible(invoice_ids) -> dict:
    """POST /apply (+ /run) — advance_reconciliation.apply_eligible (163-247).
    Rebuilds the plan server-side (never trusts client amounts); applies each
    eligible proposal, drawing from the SI-businessLine advance account first and
    overflowing to the other. Per-item savepoint so one failure never poisons the
    batch. `invoice_ids` None => apply all eligible."""
    company = _company()
    plan = _build_plan(company)
    adv, _ar = _account_maps()
    code_to_acc = {c: n for n, c in adv.items()}
    targets = [
        e for e in plan["eligible"] if invoice_ids is None or e["invoiceId"] in invoice_ids
    ]

    applied: list = []
    failed: list = []
    total_applied = _ZERO
    now = frappe.utils.nowdate()

    for idx, e in enumerate(targets):
        sp = "orion_recon_apply_%s" % idx
        frappe.db.savepoint(sp)
        try:
            si_name = e["_si"]
            remaining = _wdec(frappe.db.get_value("Sales Invoice", si_name, "outstanding_amount"))
            to_apply = min(_wdec(e["amount"]), remaining)

            bl = e["businessLine"]
            pref = "2-1220" if bl == "POIESIS_STUDIO" else "2-1210"
            codes = [pref] + [c for c in e["advances"] if c != pref]

            applied_here = _ZERO
            for c in codes:
                avail = _wdec(e["advances"].get(c, 0))
                if avail <= 0:
                    continue
                amt = min(avail, to_apply - applied_here)
                if amt <= 0:
                    break
                acc = code_to_acc.get(c)
                if not acc:
                    raise ValueError("advance account %s not found" % c)
                _post_reclass_je(
                    si_name=si_name, debit_to=e["_debit_to"], customer=e["_customer"],
                    advance_account=acc, amount=amt, business_line=bl, posting_date=now,
                    reference="ADV-APPLY %s" % si_name, code=c,
                )
                applied_here += amt

            total_applied += applied_here
            applied.append(
                {
                    "invoiceId": e["invoiceId"],
                    "invoiceNumber": e["invoiceNumber"],
                    "projectId": e["projectId"],
                    "amount": float(applied_here),
                    "newStatus": _si_status_after(si_name),
                }
            )
        except Exception as exc:  # noqa: BLE001 — collect per-item, don't abort batch
            frappe.db.rollback(save_point=sp)
            failed.append({"invoiceId": e.get("invoiceId"), "error": str(exc)})

    frappe.db.commit()
    return {
        "applied": len(applied),
        "appliedValue": float(total_applied),
        "items": applied,
        "failed": failed,
        "queued": len(plan["queue"]),
        "worklist": len(plan["worklist"]),
    }


def _apply_fifo(project_ids) -> dict:
    """POST /apply-fifo — advance_reconciliation.apply_fifo (250-344). Spreads
    each project's net advance across its OPEN invoices oldest-first (partial
    allowed), drawing from the businessLine-matching advance account first.
    `project_ids` None => all projects; otherwise legacy project ids resolved to
    ERPNext Project names."""
    company = _company()
    adv, _ar = _account_maps()
    code_to_acc = {c: n for n, c in adv.items()}
    net = _net_advance_by_project(company, adv)

    include = None
    if project_ids is not None:
        include = set()
        for pid in project_ids:
            nm = _resolve_name("Project", pid)
            if nm:
                include.add(nm)

    open_by, _all = _open_invoices_by_project(company, list(net.keys()))
    applied: list = []
    failed: list = []
    total_applied = _ZERO
    now = frappe.utils.nowdate()
    sp_i = 0  # integer-only savepoint names (SQL-safe, unlike hyphenated ids)

    for pid, by_code in net.items():
        if include is not None and pid not in include:
            continue
        open_invs = open_by.get(pid, [])  # already oldest-first (posting_date asc)
        if not open_invs:
            continue
        pool = {c: v for c, v in by_code.items() if v > 0}

        for (si, _rem) in open_invs:
            if sum(pool.values(), _ZERO) <= 0:
                break
            sp = "orion_recon_fifo_%s" % sp_i
            sp_i += 1
            frappe.db.savepoint(sp)
            try:
                remaining = _wdec(
                    frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")
                )
                if remaining <= 0:
                    continue
                to_apply = min(remaining, sum(pool.values(), _ZERO))

                bl = _bl_from_debit_to(si.debit_to)
                pref = "2-1220" if bl == "POIESIS_STUDIO" else "2-1210"
                codes = [pref] + [c for c in pool if c != pref]

                applied_here = _ZERO
                for c in codes:
                    avail = pool.get(c, _ZERO)
                    if avail <= 0:
                        continue
                    amt = min(avail, to_apply - applied_here)
                    if amt <= 0:
                        break
                    acc = code_to_acc.get(c)
                    if not acc:
                        raise ValueError("advance account %s not found" % c)
                    _post_reclass_je(
                        si_name=si.name, debit_to=si.debit_to, customer=si.customer,
                        advance_account=acc, amount=amt, business_line=bl, posting_date=now,
                        reference="ADV-APPLY %s" % si.name, code=c,
                    )
                    pool[c] = avail - amt
                    applied_here += amt

                total_applied += applied_here
                applied.append(
                    {
                        "invoiceId": _legacy_or_name("Sales Invoice", si.name),
                        "invoiceNumber": si.name,
                        "projectId": _legacy_or_name("Project", pid),
                        "amount": float(applied_here),
                        "newStatus": _si_status_after(si.name),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                frappe.db.rollback(save_point=sp)
                failed.append({"invoiceId": _legacy_or_name("Sales Invoice", si.name), "error": str(exc)})

    frappe.db.commit()
    return {
        "applied": len(applied),
        "appliedValue": float(total_applied),
        "items": applied,
        "failed": failed,
    }


@route(PR_PREFIX)
def payment_reconciliation(path: str, verb: str, payload: dict):
    """payment_reconciliation.py — GET '' (overview), GET /plan (dry run),
    POST /apply (exact match), POST /apply-fifo (per-termin), POST /run
    (daily auto-apply eligible)."""
    bare, _query = split_path(path)
    rest = bare[len(PR_PREFIX):].strip("/")
    if verb == "GET" and rest == "":
        return _overview()
    if verb == "GET" and rest == "plan":
        return _reconciliation_plan()
    if verb == "POST" and rest == "apply":
        ids = None if payload.get("applyAll") else set(payload.get("invoiceIds") or [])
        return _apply_eligible(ids)
    if verb == "POST" and rest == "apply-fifo":
        pids = set(payload.get("projectIds")) if payload.get("projectIds") else None
        return _apply_fifo(pids)
    if verb == "POST" and rest == "run":
        return _apply_eligible(None)
    frappe.throw("No compat handler for %s %s" % (verb, bare), exc=frappe.DoesNotExistError)


# ── IT: users + subscriptions (read-only) ────────────────────────────────────
#
# NOTE ON `env`: the source /api/it/users?env=staging|prod selects a per-server
# user list (cross-env proxy). Multi-env is not modelled here — this compat
# handler always returns the CURRENT site's users regardless of `env` (the query
# param is accepted and ignored). Create/edit/delete/password are out of scope
# (feynman's IT screens only need the GET list to render).


@route("/api/it/users")
def it_users(path: str, verb: str, payload: dict):
    """it/index.ts GET /users (45-70): app users as {id,name,email,role,createdAt}
    ordered createdAt asc. Coexists with the separate /api/it/health prefix."""
    if verb != "GET":
        frappe.throw("%s /api/it/users is not implemented in compat (read-only)" % verb)
    return _it_users_list()


def _it_users_list() -> list:
    """tabUser -> source user shape. Migrated users carry orion_legacy_id; the
    filter admits those OR any enabled user. Frappe roles map to ADMIN /
    PROJECT_ADMIN via orion.compat.auth.orion_role."""
    has_legacy = frappe.db.has_column("User", "orion_legacy_id")
    fields = ["name", "full_name", "email", "enabled", "creation"]
    if has_legacy:
        fields.append("orion_legacy_id")
        rows = frappe.get_all(
            "User",
            or_filters=[["orion_legacy_id", "is", "set"], ["enabled", "=", 1]],
            fields=fields,
            order_by="creation asc",
        )
    else:
        rows = frappe.get_all(
            "User", filters={"enabled": 1}, fields=fields, order_by="creation asc"
        )
    out = []
    for u in rows:
        if u.name in ("Guest",):
            continue
        out.append(
            {
                "id": (u.get("orion_legacy_id") or u.name) if has_legacy else u.name,
                "name": u.full_name or u.name,
                "email": u.email or u.name,
                "role": orion_role(u.name),
                "createdAt": _iso(u.creation),
            }
        )
    return out


@route("/api/it/subscriptions")
def it_subscriptions(path: str, verb: str, payload: dict):
    """it/index.ts GET /subscriptions (237-247): SaaS subscription list."""
    if verb != "GET":
        frappe.throw("%s /api/it/subscriptions is not implemented in compat (read-only)" % verb)
    return _it_subscriptions_list()


def _it_subscriptions_list() -> list:
    """The 'Orion Subscription' doctype was planned but does not exist in this
    deployment, and the source `subscriptions` table is empty — so this returns
    the source-shaped empty list. If/when the doctype ships, read it here and map
    to the source Subscription fields (id,name,vendor,category,description,cost,
    currency,billingCycle,startDate,expiryDate,autoRenew,specs,loginUrl,notes,
    isActive,createdAt,updatedAt)."""
    if not frappe.db.exists("DocType", "Orion Subscription"):
        return []
    rows = frappe.get_all(
        "Orion Subscription",
        fields=["*"],
        order_by="expiry_date asc",
    )
    return [_subscription_read(r) for r in rows]


def _subscription_read(r) -> dict:
    """Map an 'Orion Subscription' row (assumed snake_case fields) to the source
    Subscription shape. Fields are read defensively since the doctype is not yet
    present in this deployment."""
    return {
        "id": r.get("orion_legacy_id") or r.get("name"),
        "name": r.get("subscription_name") or r.get("name"),
        "vendor": r.get("vendor"),
        "category": r.get("category"),
        "description": r.get("description"),
        "cost": None if r.get("cost") is None else str(_wdec(r.get("cost"))),
        "currency": r.get("currency") or "IDR",
        "billingCycle": r.get("billing_cycle") or "MONTHLY",
        "startDate": _iso(r.get("start_date")),
        "expiryDate": _iso(r.get("expiry_date")) if r.get("expiry_date") else None,
        "autoRenew": bool(r.get("auto_renew")),
        "specs": r.get("specs"),
        "loginUrl": r.get("login_url"),
        "notes": r.get("notes"),
        "isActive": bool(r.get("is_active")),
        "createdAt": _iso(r.get("creation")),
        "updatedAt": _iso(r.get("modified")),
    }
