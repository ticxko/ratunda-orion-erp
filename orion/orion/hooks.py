app_name = "orion"
app_title = "Orion"
app_publisher = "PT Pencipta Organik Imaji"
app_description = "PT POI bespoke ERP logic on ERPNext"
app_email = "ticxko@gmail.com"
app_license = "mit"

required_apps = ["erpnext"]

after_install = "orion.install.after_install"

fixtures = [
    {"dt": "Role", "filters": [["name", "in", ["Orion Admin", "Orion Project Admin"]]]},
    {
        "dt": "Custom Field",
        "filters": [
            [
                "name",
                "in",
                [
                    "Account-orion_business_line",
                    "Account-orion_legacy_id",
                    "Journal Entry-orion_source_type",
                    "Journal Entry-orion_txn_hash",
                    "Journal Entry-orion_bank_account_no",
                    "Journal Entry-orion_source_file",
                    "Journal Entry-orion_legacy_id",
                    "Project-orion_business_line",
                    "Project-kn_sequence",
                    "Project-kontrak_yymm",
                    "Project-lctr",
                    "Project-orion_service_type",
                    "Project-target_margin_pct",
                    "Project-orion_program",
                    "Project-client_salutation",
                    "Project-client_phone",
                    "Project-client_address",
                    "Project-orion_legacy_id",
                    "Customer-orion_legacy_id",
                    "Supplier-orion_vendor_code",
                    "Supplier-orion_legacy_id",
                    "Lead-orion_business_line",
                    "Lead-orion_service_interest",
                    "Lead-orion_estimated_value",
                    "Lead-orion_short_name",
                    "Lead-orion_lead_status",
                    "Lead-orion_legacy_id",
                    "Bank Account-orion_owner_type",
                    "Bank Account-orion_purpose",
                    "Bank Account-orion_legacy_id",
                    "Sales Invoice-orion_legacy_id",
                    "Sales Invoice Item-orion_percentage",
                    "Payment Entry-orion_receipt_code",
                    "Payment Entry-orion_legacy_id",
                    "User-orion_legacy_id",
                ],
            ]
        ],
    },
]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "orion",
# 		"logo": "/assets/orion/logo.png",
# 		"title": "Orion",
# 		"route": "/orion",
# 		"has_permission": "orion.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/orion/css/orion.css"
# app_include_js = "/assets/orion/js/orion.js"

# include js, css files in header of web template
# web_include_css = "/assets/orion/css/orion.css"
# web_include_js = "/assets/orion/js/orion.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "orion/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "orion/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "orion.utils.jinja_methods",
# 	"filters": "orion.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "orion.install.before_install"
# after_install = "orion.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "orion.uninstall.before_uninstall"
# after_uninstall = "orion.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "orion.utils.before_app_install"
# after_app_install = "orion.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "orion.utils.before_app_uninstall"
# after_app_uninstall = "orion.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "orion.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "orion.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"orion.tasks.all"
# 	],
# 	"daily": [
# 		"orion.tasks.daily"
# 	],
# 	"hourly": [
# 		"orion.tasks.hourly"
# 	],
# 	"weekly": [
# 		"orion.tasks.weekly"
# 	],
# 	"monthly": [
# 		"orion.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "orion.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "orion.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "orion.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "orion.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["orion.utils.before_request"]
# after_request = ["orion.utils.after_request"]

# Job Events
# ----------
# before_job = ["orion.utils.before_job"]
# after_job = ["orion.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"orion.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

