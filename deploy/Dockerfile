# Layered image: stock ERPNext + the orion custom app.
# Build context = repo root (needs ./orion). HRMS joins in Phase C via the same layer.
ARG ERPNEXT_VERSION=v16.30.0
FROM frappe/erpnext:${ERPNEXT_VERSION}

COPY --chown=frappe:frappe orion /home/frappe/frappe-bench/apps/orion
RUN /home/frappe/frappe-bench/env/bin/pip install --no-cache-dir /home/frappe/frappe-bench/apps/orion
