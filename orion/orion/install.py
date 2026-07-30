import frappe

ORION_ROLES = ("Orion Admin", "Orion Project Admin")


def after_install():
    for role in ORION_ROLES:
        if not frappe.db.exists("Role", role):
            frappe.get_doc(
                {"doctype": "Role", "role_name": role, "desk_access": 1}
            ).insert(ignore_permissions=True)
    admin = frappe.get_doc("User", "Administrator")
    admin.append_roles("Orion Admin")
    admin.save(ignore_permissions=True)
    frappe.db.commit()
