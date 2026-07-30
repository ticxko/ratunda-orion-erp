# Re-export so the whitelisted path is `orion.compat.handle` (the function),
# matching the api.ts contract.
from orion.compat.handle import handle  # noqa: F401

# Import handler modules for their @route registrations.
from orion.compat import accounting  # noqa: E402,F401
