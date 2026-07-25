# Local SEO Design — rosesli.com + dodcyberconsulting.com

**Date:** 2026-07-25
**Scope:** Full local SEO for both sites. Rose SLI gets priority. Phases A and C execute now (deploy authorized); **Phase B is DEFERRED — marked for later** (per Charles 2026-07-25).
**Execution model:** Claude makes all website changes and may commit, push, and deploy as work completes (per Charles 2026-07-25: "you can deploy this"), reporting each step. Human-only steps (GBP verification, directory signups, appeals) are delivered as step-by-step packets — Word/markdown docs on Charles's Desktop, one per business.

## Context

Both sites already have solid technical SEO from May–June 2026 passes: titles, meta descriptions, canonicals, OG tags, LocalBusiness/ProfessionalService JSON-LD, sitemaps, GA4. The real local-search gaps are:

- No working Google Business Profile for either business (DOD's suspended, Rose SLI's never created)
- Zero Google reviews for either business
- Thin page coverage for buyer-intent searches (one homepage covering many services)
- No citations (Yelp, Bing Places, BBB, industry directories)

## Phase A — Local presence

### Rose SLI (first)
1. **GBP setup packet** — doc for Amanda: service-area business (no public street address), correct category, San Diego County service area, pre-written description, photo guidance. Verification is Amanda's step.
2. **Review engine** — Google review link instructions + short email/text templates Amanda sends past clients. Real reviews only; we write the ask, never the review.
3. **Citations checklist** — Yelp, Bing Places, Apple Maps, BBB, RID/interpreter directories, SD local directories, with one consistent NAP (name/address/phone) + description block so every listing matches.

*(Service landing pages moved to Phase B per Charles 2026-07-25.)*

### DOD Cyber
1. **GBP appeal packet** — phone correction steps + pre-drafted appeal form answers for the suspended profile (category already fixed to "Computer security service").
2. **Service landing pages** — dedicated CMMC Level 1 and CMMC Level 2 pages targeting the two distinct buyer types ($7.5K–12K L1 vs $40K–150K L2). Added to sitemap and site nav.
3. **Citations checklist** — Bing Places, BBB, Clutch, SDMAC directory. Cyber AB RPO registration presented as a paid decision for Charles, not executed.
4. **Review kit** — ask-templates for past/current clients.

## Phase B — Content (BUILT 2026-07-25 — DEPLOY PENDING)

> Status: all Phase B items below are built, verified locally, and committed.
> **Nothing is deployed.** rosesli commits are LOCAL-ONLY (pushing auto-deploys
> via the repo's GitHub trigger — do not push until deploy is wanted). DOD
> commits are pushed to GitHub but not deployed (no trigger; ships on next
> `gcloud builds submit`).

### Rose SLI
1. **Service landing pages** (moved from Phase A): medical, legal, and educational interpreting pages targeting "medical ASL interpreter San Diego"-style searches, with FAQ schema and internal links. Added to nav + sitemap. Respects the blog architecture rule: generated blog files are never hand-edited; `scripts/gen_blog.py` is the only writer of `/blog/<slug>` pages.
2. **Blog posts** — 2–3 new posts via `gen_blog.py` targeting question-searches Deaf clients and SD businesses actually type.

### DOD Cyber
1. **NAVFAC Nov-2026 deadline content refresh** — the existing blog post updated/expanded; urgency hook is real and searchable now.
2. **1–2 new posts** timed to the CMMC Phase 2 rollout (Nov 2026 deadline).

## Phase C — Audit & polish
1. **Lighthouse audits** on both live sites via Chrome; fix real findings (speed, mobile, accessibility).
2. **Title/meta tuning** on existing pages against target keywords.
3. **Cleanup:** orphaned `contact.html` on the DOD site still contains old fabricated services — flag for deletion, ask Charles first.
4. **Search Console:** resubmit sitemaps via existing `resubmit_sitemap.py` script; review indexing coverage.

## Target keywords

**Rose SLI (San Diego local):** "ASL interpreter San Diego", "sign language interpreter San Diego", "sign language interpreting services", "medical ASL interpreter", "legal sign language interpreter", "VRI interpreting". Market chosen: San Diego local (not SoCal-wide, not national-first).

**DOD Cyber:** "CMMC consultant San Diego", "CMMC Level 1 consultant", "CMMC Level 2 compliance", "NIST 800-171 consultant", "NAVFAC CMMC deadline".

## Ground rules
- Everything honest — no fake reviews, no invented credentials, consistent with the June 2026 honesty rewrite.
- Commit/push/deploy each reported explicitly; nothing implied as shipped that isn't.
- DOD `/js/*` changes require a `?v=` cache-bust bump on the script src in index.html (1-yr Cloudflare cache).
- Rose SLI generated blog files: never hand-edit; use `gen_blog.py`.
- Human-step packets: one doc per business, on Desktop.

## Success criteria
- Both businesses have a complete GBP packet ready for human verification steps; DOD appeal packet ready to submit.
- Review-ask kits delivered for both businesses.
- Citation checklists with consistent NAP blocks delivered for both.
- DOD L1/L2 landing pages live; Rose SLI service pages + blog posts live (Phase B).
- Lighthouse scores reviewed and material findings fixed on both sites (Phase C).
- Sitemaps current and resubmitted.
