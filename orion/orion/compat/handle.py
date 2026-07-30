"""Compat gateway: feynman keeps its legacy /api/... paths; api.ts POSTs them
here as {path, verb, payload} and unwraps Frappe's {message} envelope.

Handlers register with @route("<legacy path prefix>") and receive
(path, verb, payload). Longest prefix wins. Query strings stay in `path`
for the handler to parse (frappe.utils.parse_qs via _split_path).
"""

from urllib.parse import parse_qs, urlsplit

import frappe

ROUTES: dict[str, callable] = {}


def route(prefix: str):
    def deco(fn):
        ROUTES[prefix] = fn
        return fn

    return deco


def split_path(path: str) -> tuple[str, dict]:
    """'/api/x/y?a=1' -> ('/api/x/y', {'a': '1'}) — single values unwrapped."""
    parts = urlsplit(path)
    query = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parts.query).items()}
    return parts.path, query


@frappe.whitelist(methods=["POST"])
def handle(path: str, verb: str = "GET", payload: dict | None = None):
    if frappe.session.user in ("Guest", "", None):
        raise frappe.AuthenticationError
    bare, _query = split_path(path)
    for prefix in sorted(ROUTES, key=len, reverse=True):
        if bare == prefix or bare.startswith(prefix + "/"):
            return ROUTES[prefix](path=path, verb=(verb or "GET").upper(), payload=payload or {})
    frappe.throw(f"No compat handler for {bare}", exc=frappe.DoesNotExistError)


@route("/api/it/health")
def health(path: str, verb: str, payload: dict):
    return {
        "status": "ok",
        "site": frappe.local.site,
        "apps": {app: frappe.get_attr(f"{app}.__version__") for app in frappe.get_installed_apps()},
        "db": bool(frappe.db.sql("select 1")),
    }
