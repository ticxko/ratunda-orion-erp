#!/usr/bin/env bash
#
# extract.sh — Orion ERP data extraction (runs ON kvm4, AS ROOT)
#
# Dumps each Postgres table from database "orion_db" (schema "public",
# managed by Prisma; see ratunda-erp-orion-bellatrix/prisma/schema.prisma)
# to /opt/erpnext/migrate-data/<table>.jsonl.
#
# JSONL CONTRACT:
#   - One file per source table, named <postgres_table_name>.jsonl
#     (table names = prisma @@map values, verified against schema.prisma).
#   - One row per line, produced by Postgres row_to_json(t).
#   - JSON keys are the RAW Postgres column names exactly as defined by
#     prisma @map/@@map — importers rely on these keys verbatim.
#   - Importers read the files inside the container at
#     /home/frappe/migrate-data/ (read-only mount of /opt/erpnext/migrate-data).
#
set -euo pipefail

OUT_DIR=/opt/erpnext/migrate-data

TABLES=(
  accounts
  users
  clients
  vendors
  service_types
  leads
  programs
  projects
  project_termins
  bank_accounts
  journal_entries
  journal_lines
  invoices
  invoice_items
  invoice_payments
  bank_loans
  loan_payments
  disbursements
  disbursement_items
  spk
  spk_termins
  field_receipts
  field_receipt_items
  field_receipt_files
  office_receipts
  office_receipt_items
  office_receipt_files
  marketing_fees
  internal_vouchers
  internal_voucher_files
  voucher_types
  subscriptions
  bank_statement_jobs
)

mkdir -p "$OUT_DIR"
chmod 755 "$OUT_DIR"

for table in "${TABLES[@]}"; do
  out_file="$OUT_DIR/$table.jsonl"
  echo "Extracting public.$table -> $out_file"
  if ! sudo -u postgres psql orion_db -Atc \
      "SELECT row_to_json(t) FROM (SELECT * FROM public.$table) t" \
      > "$out_file"; then
    echo "ERROR: extraction failed for table '$table'" >&2
    exit 1
  fi
  chmod 644 "$out_file"
done

echo
echo "=== Extraction summary ==="
printf '%-30s %10s\n' "TABLE" "ROWS"
for table in "${TABLES[@]}"; do
  printf '%-30s %10s\n' "$table" "$(wc -l < "$OUT_DIR/$table.jsonl")"
done
