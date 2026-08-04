# Re-export so the whitelisted path is `orion.compat.handle` (the function),
# matching the api.ts contract.
from orion.compat.handle import handle  # noqa: F401

# Import handler modules for their @route registrations.
from orion.compat import auth  # noqa: E402,F401
from orion.compat import accounting  # noqa: E402,F401
from orion.compat import accounting_reports  # noqa: E402,F401
from orion.compat import accounting_writes  # noqa: E402,F401
from orion.compat import bank_statement  # noqa: E402,F401
from orion.compat import supply_chain  # noqa: E402,F401
from orion.compat import expense_report  # noqa: E402,F401
from orion.compat import reconciliation_it  # noqa: E402,F401
from orion.compat import crm  # noqa: E402,F401
