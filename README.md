# congress-trades

Scrapes congressional STOCK Act Periodic Transaction Reports (PTRs) from the
two official sources into Supabase, with a mobile-first dashboard.

## Data sources (provenance)
- **House**: Clerk of the House financial disclosure index
  (`disclosures-clerk.house.gov`) - yearly ZIP/XML index of filings; PTR PDFs
  fetched per DocID and parsed with pdfplumber. Scanned/paper filings are
  recorded + linked but marked `paper_skipped`.
- **Senate**: Senate eFD (`efdsearch.senate.gov`) - report index queried after
  accepting the site access agreement; electronic PTR transaction tables
  parsed from HTML. Paper filings recorded + linked, `paper_skipped`.

Every trade row stores: raw statutory amount-range string as filed, parsed
numeric bounds, direct source URL, extraction timestamp, parser version.
Nothing is silently dropped - every known filing has a `parse_status`.

## Tables (Supabase, prefix `congress_`)
`congress_filings`, `congress_trades`, `congress_scrape_runs`,
`congress_config` (all runtime knobs live here, editable from the dashboard
Config tab - no hardcoded values in code paths).

## Env vars (Render)
- `DATABASE_URL` - Postgres connection string (least-privilege `congress_app` role)
- `ADMIN_TOKEN` - required header `X-Admin-Token` for config writes + manual scrape

## Behavior
In-process scheduler scrapes every `scrape_interval_hours` (config). House PDF
queue is worked newest-first, capped at `house_max_pdfs_per_run` per cycle, so
the initial backfill catches up over successive runs.

## Known limits
- Reported trades lag reality by up to 30-45 days by law (filing deadline).
- Handwritten/scanned filings are linked but not parsed.
- House PDF parsing is heuristic; `partial`/`unparseable` statuses surface
  anything that needs eyes.
