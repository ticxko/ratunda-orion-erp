"""Session auth endpoints mirroring bellatrix /api/auth/* shapes.

feynman contract (src/lib/auth.ts):
  POST /api/auth/login  {email, password} -> {user: {id, name, email, role}}
  GET  /api/auth/me                       -> {id, name, email, role}
  POST /api/auth/logout                   -> {}
role is the legacy string ADMIN | PROJECT_ADMIN, mapped from Frappe roles.
"""

import frappe
from frappe.auth import LoginManager

ROLE_MAP = [
    ("Orion Admin", "ADMIN"),
    ("Orion Project Admin", "PROJECT_ADMIN"),
]


def orion_role(user: str) -> str | None:
    roles = set(frappe.get_roles(user))
    for frappe_role, legacy in ROLE_MAP:
        if frappe_role in roles:
            return legacy
    if "System Manager" in roles:
        return "ADMIN"
    return None


def user_payload(user: str) -> dict:
    doc = frappe.db.get_value(
        "User", user, ["name", "full_name", "email"], as_dict=True
    )
    return {
        "id": doc.name,
        "name": doc.full_name,
        "email": doc.email or doc.name,
        "role": orion_role(user),
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def login(email: str, password: str):
    lm = LoginManager()
    lm.authenticate(user=email, pwd=password)
    lm.post_login()
    return {
        "user": user_payload(frappe.session.user),
        "csrf_token": frappe.sessions.get_csrf_token(),
    }


@frappe.whitelist(methods=["GET", "POST"])
def me():
    if frappe.session.user in ("Guest", "", None):
        raise frappe.AuthenticationError
    return user_payload(frappe.session.user)


@frappe.whitelist(methods=["POST"])
def logout():
    frappe.local.login_manager.logout()
    frappe.db.commit()
    return {}
