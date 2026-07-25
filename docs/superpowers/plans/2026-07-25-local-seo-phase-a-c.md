# Local SEO Phase A + C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the local-SEO foundation for rosesli.com and dodcyberconsulting.com — GBP/citation/review packets for both businesses, CMMC L1/L2 landing pages on the DOD site, and an audit/title-tuning pass — with deploys. Phase B (Rose SLI service pages, blog content) is DEFERRED.

**Architecture:** Both sites are static HTML served by small Flask apps on Cloud Run (manual `gcloud builds submit` deploys, no GitHub trigger). Human-only steps (GBP verification, appeals, directory signups) are delivered as packet docs on Charles's Desktop, one per business.

**Tech Stack:** Static HTML + Flask (`main.py` route-per-page), Cloud Run, chrome-devtools MCP for Lighthouse, `python-docx` (optional) for Word conversion.

## Global Constraints

- Everything honest — no fake reviews, no invented credentials (June 2026 honesty rewrite stands).
- Report every commit, push, and deploy explicitly; never imply shipped when not.
- Deploy authorized for Phases A and C (Charles 2026-07-25).
- DOD repo: `C:\My-Website`; Rose SLI repo: `C:\Users\Charles\github\rosesli-website`.
- DOD `index.html` uses CRLF line endings; `blog.html` uses LF — preserve per-file endings.
- DOD `/js/*` is Cloudflare-cached 1 yr — any JS change requires bumping `?v=` on the script src in `index.html` (no JS changes planned).
- Rose SLI generated blog files under `blog/` are never hand-edited (not touched in this plan).
- Deploys: `gcloud builds submit` from each repo root per its `cloudbuild.yaml` (see `deploy.sh` in C:\My-Website).
- Business data (NAP): **Rose SLI** = "Rose Sign Language Interpreting", +1-858-263-6719, info@rosesli.com, San Diego CA (service-area business, no public street address), https://rosesli.com. **DOD** = "DoD Cyber Consulting", 4485 Manitou Way, San Diego CA 92117-2845, +1-619-943-1225 (Charles must confirm before publishing), https://dodcyberconsulting.com, Calendly https://calendly.com/rosecharlesrose.

---

### Task 1: Rose SLI local SEO packet (Desktop doc)

**Files:**
- Create: `C:\Users\Charles\Desktop\RoseSLI_Local_SEO_Packet.md` (+ `.docx` if `python-docx` importable)

**Interfaces:**
- Produces: the canonical Rose SLI NAP block reused verbatim by any future listing work.

- [ ] **Step 1: Write the packet** with these sections, fully written out (no outlines):
  1. **Canonical NAP block** — exact name/phone/email/site/service-area/description (≤750 chars, keyword: "certified ASL interpreters", "San Diego County") to paste into every listing.
  2. **Google Business Profile setup** — numbered steps: business.google.com → create profile as *service-area business* (hide address, set service area = San Diego County + named cities: San Diego, Chula Vista, Oceanside, Escondido, Carlsbad, El Cajon); primary category "Interpreter" (search picker for "sign language" variants and prefer one if offered); hours; description (pre-written); photos guidance (logo, Amanda at work, hands/ASL imagery already on the site); services list (medical, legal, educational, mental health, performance, VRI); verification note (video or postcard — Amanda's step).
  3. **Review engine** — how to get the review short-link from the GBP dashboard once verified; 2 ask-templates (email + text) for past clients, warm tone, one-tap link, no incentives; cadence guidance (2–3 asks/week, steady beats burst).
  4. **Citations checklist** — table: Yelp, Bing Places, Apple Business Connect, BBB, RID member directory (rid.org), Nextdoor, local SD directories — each with URL, what to paste (the NAP block), and free/paid flag.
- [ ] **Step 2: Convert to Word if possible**: `python -c "import docx"` — if it imports, generate the `.docx` alongside; if not, deliver `.md` only and say so.
- [ ] **Step 3: Verify** the file exists on Desktop and every section from Step 1 is present (no TBDs).

### Task 2: DOD Cyber local SEO packet (Desktop doc)

**Files:**
- Create: `C:\Users\Charles\Desktop\DODCyber_Local_SEO_Packet.md` (+ `.docx` if available)

- [ ] **Step 1: Write the packet** with these sections:
  1. **Canonical NAP block** (address as in Global Constraints; phone flagged "confirm +1-619-943-1225 is the number you want public — it's the Twilio line").
  2. **GBP reinstatement** — current state (suspended; category already fixed to "Computer security service"); numbered steps: fix the phone on the profile to match the site/NAP, then submit the appeal at support.google.com/business (Appeals tool) with pre-drafted answers: business description, why it's legitimate (registered business, real address, live site), evidence list (website, business license/registration doc, utility bill or bank statement showing name+address). Include the pre-drafted appeal statement text in full.
  3. **Review kit** — email template asking past/current CMMC clients for a Google review (works only post-reinstatement) + LinkedIn recommendation ask as the interim social proof.
  4. **Citations checklist** — Bing Places, Apple Business Connect, BBB, Clutch.co, SDMAC member directory, Yelp — same table format as Task 1.
  5. **Cyber AB RPO — decision for Charles** — what it is (the recognized CMMC ecosystem directory), that competitors are absent from it (Data Net/RJE not registered), approximate cost ($1,000/yr range — verify current fee at cyberab.org), and why it's the single strongest trust signal in this market. Present as YES/NO decision, not executed.
- [ ] **Step 2: Convert to Word if possible** (same check as Task 1).
- [ ] **Step 3: Verify** file exists, all 5 sections present.

### Task 3: DOD CMMC Level 1 + Level 2 landing pages

**Files:**
- Create: `C:\My-Website\cmmc-level-1.html`, `C:\My-Website\cmmc-level-2.html`
- Modify: `C:\My-Website\main.py` (two new routes, after the `/cmmc-guide` route at ~line 261)
- Modify: `C:\My-Website\index.html` (footer links to both new pages)
- Modify: `C:\My-Website\sitemap.xml` (two new `<url>` entries, priority 0.9)

**Interfaces:**
- Produces: routes `GET /cmmc-level-1` and `GET /cmmc-level-2` each serving its static file; used by Task 4 (deploy verify) and Task 6 (sitemap resubmit).

- [ ] **Step 1: Build both pages** using `blog/navfac-cmmc-deadline-2026.html` as the structural template (same nav/footer/styles/GA4), with this content:
  - **L1 page** — `<title>CMMC Level 1 Consultant — Small DOD Subcontractors | San Diego</title>`; meta description ~155 chars w/ "CMMC Level 1 consultant San Diego"; H1 "CMMC Level 1 Compliance for Small DOD Subcontractors"; sections: who needs L1 (FCI-only contracts, FAR 52.204-21 basic safeguarding, annual self-assessment + SPRS affirmation), what we do (gap assessment → remediation plan → SPRS submission support), honest pricing band ($7.5K–$12K, 2–6 weeks), FAQ (3–4 real questions: "Do I need a third-party assessment for Level 1?" No — self-assessment; "What's the deadline?"; "What if I also handle CUI?" → link to L2 page), CTA (Calendly + /#contact). JSON-LD: `Service` + `FAQPage`.
  - **L2 page** — `<title>CMMC Level 2 Compliance Consultant — NIST 800-171 | San Diego</title>`; H1 "CMMC Level 2 / NIST SP 800-171 Compliance for DOD Contractors"; sections: who needs L2 (CUI, DFARS 252.204-7012), the 110 controls, C3PAO third-party assessment for most contracts, **NAVFAC SW Nov 10 2026 deadline hook**, phased engagement (gap → POA&M → remediation → assessment prep), pricing band ($40K–$150K+, 3–9 months), FAQ, CTA. JSON-LD: `Service` + `FAQPage`.
  - All claims consistent with the honesty rewrite — no certifications or client counts we don't have.
- [ ] **Step 2: Add routes** in `main.py` mirroring the `/cmmc-guide` pattern (`send_from_directory(BASE_DIR, "cmmc-level-1.html")` etc.).
- [ ] **Step 3: Add footer links** on `index.html` to both pages (anchor text "CMMC Level 1" / "CMMC Level 2") and cross-link the two pages to each other; add both to `sitemap.xml` with `lastmod` = today.
- [ ] **Step 4: Verify locally**: `python -c` smoke test with Flask test client — `GET /cmmc-level-1` and `/cmmc-level-2` return 200 and correct `<title>`; validate `sitemap.xml` well-formedness with `defusedxml` if importable, else `xml.etree.ElementTree` (first-party local file, not untrusted input).
- [ ] **Step 5: Commit** both pages + routes + sitemap + footer in one commit.

### Task 4: Deploy DOD site (Phase A ship)

- [ ] **Step 1: Push** `C:\My-Website` to origin.
- [ ] **Step 2: Deploy**: `gcloud builds submit` per `deploy.sh` / `cloudbuild.yaml`.
- [ ] **Step 3: Verify live**: request `https://dodcyberconsulting.com/cmmc-level-1` and `/cmmc-level-2` via PowerShell `Invoke-WebRequest` (sandbox curl gives false 000) — expect 200 + correct titles.
- [ ] **Step 4: Report** commit/push/deploy status explicitly.

### Task 5: Lighthouse audit + fixes (Phase C)

**Files:**
- Modify: whatever the audits implicate (record before changing).

- [ ] **Step 1: Audit** both live homepages with chrome-devtools `lighthouse_audit` (mobile). Record scores: Performance / Accessibility / Best Practices / SEO.
- [ ] **Step 2: Triage** — fix findings that are real and cheap (missing alt text, contrast, meta issues, uncompressed images, render-blocking easily deferred). Skip architecture rewrites. List skipped items with reasons.
- [ ] **Step 3: Commit fixes** per repo (if any).

### Task 6: Title/meta tuning (Phase C)

**Files:**
- Modify: `C:\My-Website\index.html` (title), `C:\Users\Charles\github\rosesli-website\about.html`, `vri.html`, `request.html`, `testimonials.html` (titles + descriptions where thin)

- [ ] **Step 1: DOD homepage title** → `CMMC Compliance Consultant San Diego | DoD Cyber Consulting` (current title is the brand tagline with zero search terms).
- [ ] **Step 2: Rose SLI titles** →
  - about: `About Amanda Rose — Certified ASL Interpreter in San Diego | Rose SLI`
  - vri: `Video Remote Interpreting (VRI) Services — Rose SLI, San Diego`
  - request: `Request an ASL Interpreter in San Diego — Rose Sign Language Interpreting`
  - testimonials: `Client Testimonials — Rose Sign Language Interpreting, San Diego`
  Update each page's meta description to match if it lacks the keyword/city.
- [ ] **Step 3: Verify** no title exceeds ~60 chars badly (65 hard max), descriptions 140–160 chars.
- [ ] **Step 4: Commit** per repo.

### Task 7: DOD contact.html cleanup (Phase C — ASK FIRST)

- [ ] **Step 1: Confirm with Charles** (AskUserQuestion): `/contact` already 301-redirects to `/#contact`; `contact.html` is unrouted and still contains pre-rewrite fabricated services. Propose deleting it.
- [ ] **Step 2: If approved**, `git rm contact.html`, verify no reference to it remains (`grep -r "contact.html" main.py *.html`), commit.

### Task 8: Sitemaps + Search Console + final ship

- [ ] **Step 1: Bump `lastmod`** on changed pages in both sitemaps.
- [ ] **Step 2: Commit + push** any remaining Phase C changes in both repos.
- [ ] **Step 3: Deploy** whichever repos changed in Phase C (`gcloud builds submit` each).
- [ ] **Step 4: Resubmit sitemaps**: `python C:\Users\Charles\Documents\ai-stack\scripts\resubmit_sitemap.py` — if it errors on the SC owner gate (service account must be added as Owner per property), report that as Charles's manual step, don't retry.
- [ ] **Step 5: Verify live** changed pages via PowerShell `Invoke-WebRequest`; report full commit/push/deploy status for both repos.
