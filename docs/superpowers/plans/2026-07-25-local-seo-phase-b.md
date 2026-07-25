# Local SEO Phase B Implementation Plan (build only — NO DEPLOY)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Build Phase B content for both sites, commit but do NOT deploy. Mark everything deploy-pending and record it in the Desktop packets.

**Deploy policy (Charles 2026-07-25):** NO deploys. rosesli-website has a GitHub Cloud Build trigger that auto-deploys on push → **rosesli commits stay LOCAL (no push)**. DOD repo has no trigger → commit + push allowed, no `gcloud builds submit`.

## Global Constraints
- Same honesty/NAP/keyword rules as the Phase A+C plan (`2026-07-25-local-seo-phase-a-c.md`).
- Rose SLI blog: articles are `<article>` blocks in `blog.html`; standalone pages generated ONLY by `scripts/gen_blog.py` (add SLUGS entry, run script, paste its sitemap output). Never hand-edit `blog/*.html`.
- DOD blog: static files under `blog/`; new slugs must be whitelisted in `main.py`; card added to `blog.html`; sitemap entry added. Use `?v=2` for blog-post.css.

### Task 9: Rose SLI service landing pages
- Create `medical-interpreting.html`, `legal-interpreting.html`, `educational-interpreting.html` using `vri.html` as chrome template.
- Targets: "medical ASL interpreter San Diego", "legal sign language interpreter San Diego", "educational ASL interpreter / IEP interpreter San Diego".
- Each: keyworded title ≤65 chars, meta description 140–160, H1 + sections (who it serves, how Rose SLI handles it, why certified/specialty matters, FAQ ×3), Service + FAQPage + Breadcrumb JSON-LD, cross-links (each other, /vri, /request, relevant blog posts), link from /specialties + footer/nav where the site pattern allows.
- Routes in `main.py` mirroring `/vri` pattern; sitemap entries priority 0.8, lastmod today.
- Verify: Flask test client 200 + title on all three; JSON-LD parses; sitemap well-formed. Commit locally. DO NOT PUSH.

### Task 10: Rose SLI blog posts (2)
- Post A: "Who pays for an ASL interpreter? What the ADA says for San Diego businesses" (slug `who-pays-asl-interpreter-ada`).
- Post B: "How to request an ASL interpreter for a medical appointment in San Diego" (slug `request-asl-interpreter-medical-appointment`).
- Add `<article>` blocks to `blog.html` following existing structure (anchor id, date, body), add SLUGS entries newest-first, run `python scripts/gen_blog.py`, paste emitted sitemap blocks into `sitemap.xml`.
- Verify: generated files exist, hub cards render, test client 200 on both slugs. Commit locally. DO NOT PUSH.

### Task 11: DOD content
- Refresh `blog/navfac-cmmc-deadline-2026.html`: update dateModified + add a short 2026-07 status paragraph (Phase 2 now ~3.5 months out; tie to /cmmc-level-2). Bump sitemap lastmod.
- New post `blog/cmmc-level-1-vs-level-2.html`: "CMMC Level 1 vs Level 2: Which One Does Your Contract Require?" — decision-focused, links both landing pages, FAQ ×3, Article+FAQPage JSON-LD, blog-post.css?v=2, whitelist slug in `main.py`, card on `blog.html`, sitemap entry.
- Verify: test client 200s, JSON-LD parses. Commit + push. NO DEPLOY.

### Task 12: Mark + packets
- Append "Phase B — built, awaiting deploy" section to both Desktop packet .md files listing exactly what ships on next deploy; regenerate .docx.
- Update spec Phase B header to BUILT/DEPLOY-PENDING; update memory checkpoint.
