# RoseSLI Portal — Feature Batch (Charles, 2026-07-22)

This is the **verbatim, complete** request, decomposed into phases. It is the
source of truth for this work round — do not rely on conversation summaries.
Update the Status column as items are verified/built. Never delete an item.
Continues the numbering convention from `portal-feature-batch-2026-06-20.md`
(that batch's outstanding items — #15 totals-math — are resolved in Phase 5
below).

Legend: ⬜ not started · 🟡 in progress · ✅ done & verified · ❓ needs clarification

## Resolved decisions (from clarifying questions, 2026-07-22)
- **Interpreter Review model**: two separate surfaces. The interpreter's own
  create/edit/lock/submit flow lives on the existing **Interpreter Invoices**
  list (`/portal/invoices`) — no new page for interpreters. The existing
  admin-only **Interpreter Review** page (`/portal/admin/interpreter-review`)
  stays admin-only and just receives what gets submitted, unchanged in access model.
- **Send Offer blast**: additive. Targeted offer-to-specific-interpreter(s)
  (existing) stays as-is; "Broadcast to all interpreters" is a new second
  action, not a replacement.
- **Phone number**: rosesli.com only — DOD site has no displayed number.
  ✅ Already done (this session): 619-289-7368 → 858-263-6719 replaced in 20
  files (all templates, gen_blog.py, JSON-LD). Not yet committed/pushed/deployed.
- **Pacing**: phased, one phase executed and shipped per session, same as
  the 2026-06-20 and 2026-07-09 batches. Nav restructure (Phase 10) goes last
  because it links to pages/positions this batch builds.

## Phase 0 — Website: phone number ✅ (done, uncommitted)
| # | Requirement | Status |
|---|---|---|
| 0.1 | Replace 619-289-7368 → 858-263-6719 everywhere on rosesli.com | ✅ done locally, needs commit/push/deploy approval |

## Phase 1 — Interpreter Invoice lifecycle + Expenses
| # | Requirement | Status |
|---|---|---|
| 1.1 | Per individual invoice: auto-create a separate line under "rate applied" for each differential that applies to the job's hours; auto-apply the differential to the correct hours (not a manual add) | ✅ new `/portal/api/time-bands` endpoint wraps `portal_rates.compute_time_band_hours`; form auto-splits Date/Start/End into differential lines, tagged `data-auto` so re-splitting only touches auto lines |
| 1.2 | Total line: sum all hours + sum all dollars across the invoice's lines | ✅ fixed pre-existing bug where line totals never wrote back to Amount; added visible Total row (hrs + $) on create/edit and detail view |
| 1.3 | "Edit Invoice" button → links back to invoice creation/edit page | ✅ `/portal/invoices/<id>/edit` (owner-employee while unsubmitted, admin always) merged into `create_invoice()` |
| 1.4 | "Submit for Review" button: locks the invoice from further interpreter edits; notifies admin's Interpreter Review page (email + appears there) | ✅ edit route blocks non-admins once `submitted`; submit + submit-batch both email coordinators (`send_invoice_submitted_email`) |
| 1.5 | Individual invoice: add "Expenses" section — dropdown (Parking / Mileage / Travel Time / Other) + free-text box next to it | ✅ dropdown + note, stored as JSON in `invoices.expenses` (migration 019, this session's origin commit) — informational only, never priced/rolled into total per that commit's explicit decision |
| 1.6 | Interpreter Invoices list page: split into top section (not-yet-submitted, editable) and bottom section (past/submitted, read-only) | ✅ employee view only |
| 1.7 | Master invoice list: checkboxes per invoice, multi-select, "Submit" button submits all checked invoices at once | ✅ new `/portal/invoices/submit-batch`, scoped to caller's own open invoices |
| 1.8 | "Create Invoice" action → new invoice flows into the not-yet-submitted (top) section | ✅ create redirects straight to the new invoice's detail page; employees can optionally link one of their unbilled jobs (`job_id`, mirrors the master-invoice exclusion so the same job can't be billed both ways) |

**Built 2026-07-23.** 24 tests in `tests/test_phase9_invoice_lifecycle.py` (schema/time-band-splitter tests were already present from the earlier same-day commit; this session added the route/UI-wiring tests). Full suite 242 passed / 3 pre-existing unrelated fails (2 known MFA reds + 1 stale hardcoded-date test). Migration 019 (`019_invoice_job_link.sql`) already on prod per that earlier commit — no new migration this session. Not yet committed/pushed/deployed.

## Phase 2 — Client Review page (new)
| # | Requirement | Status |
|---|---|---|
| 2.1 | New "Client Review" page, using the Interpreter Review page's format/layout | ✅ `/portal/admin/client-review`, checkbox-table layout mirroring Interpreter Invoices/Review |
| 2.2 | Imports from the Client Invoice page/data | ✅ queries `client_invoices` directly, same detail page (`/portal/client-invoices/<id>`) on row click |
| 2.3 | Requires client review before an invoice can be submitted | ✅ new `submitted` flag (migration 020, DEFAULT TRUE backfill); new invoices created FALSE and are invisible to the client (list + detail both filtered) until an admin submits them here |
| 2.4 | Checkbox per invoice to select for submission | ✅ select-all + per-row checkboxes → `/portal/admin/client-invoices/submit-batch` |

**Built 2026-07-23.** 10 tests in `tests/test_phase2_client_review.py`. Full suite 252 passed / 3 pre-existing unrelated fails (2 known MFA reds + 1 stale hardcoded-date test). Migration 020 (`020_client_invoice_review.sql`) applied to prod before deploy. Committed/pushed/deployed.

## Phase 3 — Interpreter email list
| # | Requirement | Status |
|---|---|---|
| 3.1 | Build "Interpreter email list" by pulling all emails from interpreter profiles (feeds Phase 6 blast) | ✅ `portal_offers.active_interpreter_emails(company)` — active, non-archived interpreters only; no new page (the existing Interpreter Profiles page at `/portal/admin/interpreters` already lists emails for humans — this is the server-side list Phase 6's blast will consume) |

**Built 2026-07-23.** 3 tests in `tests/test_phase3_interpreter_email_list.py`. Backend-only — nothing to deploy until Phase 6 wires it into the blast.

## Phase 4 — Administrative Assignments form rework
| # | Requirement | Status |
|---|---|---|
| 4.1 | Add "Administrative Assignments" section/shortcut under Dashboard | ✅ new stat-card on the admin dashboard → `/portal/admin/assignments/new` |
| 4.2 | Location: remove "Deaf Client", replace with "Consumer(s):" + a +/− button to set the number of consumers | ✅ removed the leftover `deaf_clients` text field (the real Consumer name+email rows already existed from an earlier batch); added a "−" remove button per row, kept "+ Add consumer" |
| 4.3 | Remove "Interpreter" search field; add "+ Interpreter" button with a dropdown of available interpreters | ✅ removed the free-text search box; dropdown now filters to interpreters free that day via `portal_availability.available_interpreters`, live-refetched from `/api/interpreters?date=` on every Date change (clarified w/ Charles: real availability filter, not just search removal) |
| 4.4 | One line per required interpreter (ties into existing multi-interpreter `job_interpreters` model) | ✅ already built in an earlier batch — untouched |
| 4.5 | "Client" becomes a dropdown that auto-populates fields from the client profile | ✅ already a dropdown (rate auto-fill existed); added POC name/email/phone auto-fill from the client account (clarified w/ Charles), same don't-clobber-manual-edits behavior |
| 4.6 | Remove "Setting" field | ✅ removed from the form; `_parse_job_form` no longer writes the column at all, so existing values on old assignments aren't wiped by a later edit |
| 4.7 | "Format" becomes a dropdown | ✅ In-Person / VRI / VRS / Phone-OPI (clarified w/ Charles) |
| 4.8 | "Dress code" becomes a dropdown | ✅ Business Professional / Business Casual / Casual / Scrubs-Medical (clarified w/ Charles) |
| 4.9 | Remove "Client address" and "Client name" fields | ✅ removed from the form; client_name still derives from the linked account server-side, client_address column left untouched on edit (same preserve-legacy approach as Setting) |
| 4.10 | Add POC name + POC email fields | ✅ already built in an earlier batch — untouched |

**Built 2026-07-23.** Also fixed a latent bug found while touching this form: the assignment-form `<script>` had no CSP nonce, so under this portal's strict nonce-only `script-src` it was silently non-functional in production (Add Interpreter/Add Consumer/rate auto-fill never worked). 11 tests in `tests/test_phase4_admin_assignments_rework.py` (named for the batch, not "test_phase4.py" — that file already exists covering an unrelated earlier-batch "Phase 4"). Full suite passed / same 3 pre-existing unrelated fails.

## Phase 5 — Billing section
| # | Requirement | Status |
|---|---|---|
| 5.1 | Admin-only access | ⬜ |
| 5.2 | Apply differentials | ⬜ |
| 5.3 | Auto-calculate + display total billed = client rate × duration when hourly | ⬜ |
| 5.4 | If flat rate, display flat-rate total only (no rate×duration math) | ⬜ |
| 5.5 | Resolves 2026-06-20 batch's open #15: differential now wires into the main line total, not just an additive extra line | ⬜ |

## Phase 6 — Send Offer blast
| # | Requirement | Status |
|---|---|---|
| 6.1 | New "Broadcast to all interpreters" action (additive to existing targeted offer) | ⬜ |
| 6.2 | Blast email includes Schedule section info + zip code only (not full address) | ⬜ |

## Phase 7 — Job Offers visibility
| # | Requirement | Status |
|---|---|---|
| 7.1 | Verify/enforce Job Offers page visible to admin + interpreters only (clients already excluded — add explicit guard/test) | ⬜ |

## Phase 8 — "Needs staffing" interpreter-facing rework
| # | Requirement | Status |
|---|---|---|
| 8.1 | Remove "Client" column | ⬜ |
| 8.2 | Show assignment date + "Start Time – End Time" | ⬜ |
| 8.3 | Location displayed as zip code only | ⬜ |
| 8.4 | Show "Type of assignment" field | ⬜ |
| 8.5 | Add "Accept" / "Decline" checkboxes | ⬜ |
| 8.6 | Accept → email interpreter (from profile) with: Schedule info, Location info, Interpreters assigned (or "Unassigned"), client name, notes for interpreters, document attachments | ⬜ |
| 8.7 | Accept → populates the assignment's "Interpreter" field with that interpreter's name | ⬜ |
| 8.8 | Decline → greys out that interpreter's name in the assignment's Interpreter dropdown, for that assignment only | ⬜ |

## Phase 9 — Job # everywhere
| # | Requirement | Status |
|---|---|---|
| 9.1 | Surface a consistent Job # (existing `jobs.id`) on every assignment-related view: detail, invoices, offers, calendar, emails | ⬜ |

## Phase 10 — Main menu / nav restructure (LAST — depends on Phases 1–9 existing)
| # | Requirement | Status |
|---|---|---|
| 10.1 | Delete "Create Interpreter Invoice" standalone nav link | ⬜ |
| 10.2 | Delete "Create Client Invoice" standalone nav link | ⬜ |
| 10.3 | Add "Client Review" nav link (Phase 2 page) | ⬜ |
| 10.4 | Move "Admin Assignments" to the top of the Admin section | ⬜ |
| 10.5 | Remove "Invite User" | ⬜ |
| 10.6 | Move "Availability" to sit under "Profile" | ⬜ |
| 10.7 | Move "Interpreter Invoices" + "Client Invoices" + "Profile" into the Admin section (Interpreter Invoices positioned near "Incoming Requests") | ⬜ |

**Assumption to verify at Phase 10 time**: removing the "Create Interpreter/Client Invoice" nav links assumes invoice creation still happens contextually (interpreter creates their own via Phase 1; client invoice auto-creates on assignment, already shipped). If admins still need a manual "create invoice for someone" affordance, we'll add a button on the relevant list page instead of a standalone nav link.

## Progress log
- 2026-07-22: Batch doc created after brainstorming + clarifying questions. Phase 0 (phone number) done locally, pending commit/push/deploy approval.
