# Pre-Cutover Cleanup Review — items needing owner/bookkeeper decisions

None of these block the ongoing ERPNext build on staging. All of them must be
resolved (or explicitly accepted) **before Cutover A**, because the final
production import re-runs from a fresh source snapshot.

## 1. Invoice reject cohort (11 invoices) — confirm archive-only treatment
`invoice_rejects.json` (staging site, `private/migrate-maps/`) lists invoices
that could not become GL-posting Sales Invoices:
- Several PAID invoices (KN02/KN03-era, e.g. `R-INV01-KN02-26III-LCTR-RVAR`)
  whose only AR journal entry is an advance-application (Dr 2-12xx / Cr AR) —
  no true "send" JE exists; revenue reached the GL via direct bank-import
  entries instead.
- Retroactive reconstruction records (`recon260728_jade_inv06`, `R-INV04..06-KN02`…)
  with no AR JE at all.
- One CANCELLED invoice (`P-INV03-KN06-25III-HILL-INTDSGN`).
**Decision needed**: accept these as archive-only (JEs remain the GL truth,
no ERPNext Sales Invoice) — or manually reconstruct send JEs in source first.
Also: 5 payment records reference no journal entry anywhere (no GL exists);
they are excluded from AR expectations and listed in the same file.

## 2. DBS KTA loan — events don't support the cached balance
Recomputed outstanding from loan events = **0**, source cached balance =
**Rp 141,666,668**. This is the crosscheck doc's known 2-1700/Treatment-B
double-count issue (decision of 26 Jul). The loan's drawdown/repayment event
register needs reconstruction in source (or a signed adjustment) so
outstanding recomputes correctly. (Lastiko's loan is clean: recomputes to
exactly Rp 224,626,668 = subledger.)
Also: 7 loan events had no docCode in source; codes `PTPOI-CL11-26VII` …
`CL19` were generated during import — verify they don't clash with any paper
records.

## 3. Account 6-1600 direct postings (11 lines)
`6-1600 Beban Marketing, Referral & Promosi` carries 11 journal lines from
before its Ratunda(6-1601)/Poiesis(6-1602) mirror split was finished. In
ERPNext it became a group + technical leaf `6-1600.1` holding those lines.
**Decision needed**: reclassify the 11 lines into 6-1601/6-1602 (in source
pre-cutover, or in ERPNext post-cutover), then retire 6-1600.1.
Note: 6-1601 currently sits at top level (parent 6-0000) while 6-1602 sits
under 6-1600 — the tree itself is inconsistent in source.

## 4. 2025 double-imports — the big one (crosscheck doc)
GL 2-1510 shows 53.5M vs owner-loan subledger 224.6M due to un-reconciled
2025 double-imports, plus negative bank GL balances (1-1150 Xpresi,
1-1130 Livin) from missing statement imports.
**Process** (plan §4 cleanup pass): run the dedup tooling over 2025 → produce
exclusion CSV → bookkeeper signs → transform skips those JEs. Missing
statements: import them into Orion, or book adjusting JEs to a
`1-1990 Suspense Migrasi` account. Missing Amita loan register (2-1520 =
89.76M): create the loan + backfill events (owner review).

## 5. Minor (no decision, just awareness)
- Two different clients named "Anggi Aisyah" → second is Customer
  "Anggi Aisyah (2)".
- Round-off account `8-9990 Pembulatan` was added to the CoA (ERPNext
  requirement; receives no postings).
