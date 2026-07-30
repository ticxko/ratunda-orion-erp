"""Step 3 — users.

users.jsonl columns: id, name, email, password (ignored), role.
Existing users (e.g. the wizard's Administrator/first user) are not
recreated — they just get orion_legacy_id stamped so the count gate holds.
Passwords are NOT migrated; frappe leaves accounts password-less until a
reset.
"""

import frappe

from orion.migrate import load_jsonl, start_import

ROLE_MAP = {"ADMIN": "Orion Admin", "PROJECT_ADMIN": "Orion Project Admin"}


def run():
	start_import()
	created = 0
	patched = 0
	for r in load_jsonl("users"):
		email = (r["email"] or "").strip().lower()
		if not email:
			continue

		if frappe.db.exists("User", email):
			if not frappe.db.get_value("User", email, "orion_legacy_id"):
				frappe.db.set_value("User", email, "orion_legacy_id", r["id"], update_modified=False)
				patched += 1
			continue

		user = frappe.new_doc("User")
		user.email = email
		user.first_name = r["name"]
		user.enabled = 1
		user.user_type = "System User"
		user.orion_legacy_id = r["id"]
		user.append("roles", {"role": ROLE_MAP.get(r["role"], "Orion Project Admin")})
		if frappe.db.exists("Role", "Desk User"):
			user.append("roles", {"role": "Desk User"})
		user.flags.no_welcome_mail = True
		user.flags.ignore_permissions = True
		user.insert()
		created += 1

	frappe.db.commit()
	print("users: created %s, patched %s existing" % (created, patched))
