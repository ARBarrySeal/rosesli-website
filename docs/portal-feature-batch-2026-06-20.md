# RoseSLI Portal — Authoritative Feature Batch (Charles, 2026-06-20)

This is the **verbatim, complete** request. It is the source of truth for this
work round. Do NOT rely on conversation summaries — this file is canonical.
Update the Status column as items are verified/built. Never delete an item.

Legend: ⬜ not started · 🟡 partial/in-progress · ✅ done & verified · ❓ needs clarification

## Audit result (2026-06-20)
**26 of 30 already built & verified** in the Phase 1–6 batch (commit 81aaf00).
Genuinely outstanding: **#12** (admin Job Offers grouped by category — "Gap 3"),
**#15** (differential is manual, not auto-selected from times). Two need a
decision from Charles before building: **#1** and **#3**.

| # | Requirement | Status | Evidence / Notes |
|---|-------------|--------|------------------|
| 1 | Page **Interpreter Review** listing all info interpreters submit on interpreter-invoice main page | ✅ | NEW admin page `/portal/admin/interpreter-review` (portal_interpreter_invoices.py) → `portal_interpreter_review.html`; lists every submitted master invoice (interpreter, #assignments, total, status, submitted date), row → existing full breakdown. Nav link added (rosesli admin). |
| 2 | **Master invoice**: job#, client info, assignment info, attachments; rates × time w/ subtotals + total; incidentals line | ✅ | `invoice_detail` + `invoice_jobs` lines + `invoice_attachments` (portal_pages.py:817) |
| 3 | Interpreters **select invoices (radio) + Submit → payment** | 🟡 | `portal_pay_review.html` does checkbox-select + submit → `/portal/pay/submit`. "to payment" target undefined (no processor wired). Clarify. |
| 4 | Create-client-invoice: rate from client profile; differentials dropdown; hours+amount per differential | ✅ | `portal_client_invoice_create.html` DIFF_OPTIONS + recomputeLine |
| 5 | **Base rate** on interpreter profile | ✅ | `interpreter_rate` field, portal_pages.py:224 |
| 6 | Clients + interpreters **auto-logout 30 min idle** | ✅ | portal_base.html:139-152 |
| 7 | Assignment page: **email assignment info to selected interpreters** | ✅ | `offer_job` → `send_offer_email` (portal_offers.py:131) |
| 8 | Edit assignment: populate interpreter # with **interpreter name** | ✅ | `interpreter_1_name = _name_for(...)` portal_jobs.py:198 |
| 9 | Confirm → emails interpreter; Withdraw → "You have been unassigned…" email | ✅ | `send_confirm_email` + `send_withdraw_email` (portal_offers.py) |
| 10 | **Duration** computed from start/end | ✅ | `_compute_duration` portal_jobs.py:130,164 |
| 11 | New assignment: pull client rate from client profile | ✅ | portal_jobs.py:167-174 |
| 12 | **Admin-only Job Offers page** shows all assignments in categories | ✅ | `/portal/offers` now branches: admins get `_admin_offers_view` → `portal_offers_admin.html`, all assignments grouped by status (Needs staffing / Confirmed / Completed / Cancelled) with open-offer counts; row → assignment detail. |
| 13 | Create-client-invoice: auto-fill rate/hr from client profile | ✅ | client-select change → data-rate (template:76-82) |
| 14 | Subtotal = rate × duration; total = subtotal + incidentals | ✅ | template formula + server total |
| 15 | **Auto-select differential** from start/end times; amount = (rate+diff)×duration | 🟡 | Auto-SELECT now DONE: added-lines preselect the differential from date (weekday/weekend) + start-time band (`suggestedDiffIndex` in portal_client_invoice_create.html). REMAINING decision: making it the *main-line* total (amount = (rate+diff)×duration) conflicts with current model where extra lines add ON TOP of base duration×rate — that's a billing-math change, see clarifications. |
| 16 | Create-client-invoice: auto-fill date of service + time from request | ✅ | `_prefill` / `pf` job_id prefill (template:10,26-28) |
| 17 | **Save + Cancel** at bottom of availability page | ✅ | portal_availability.html:80-83 |
| 18 | Uploaded document → corresponding profile | ✅ | `target_user_id` upload + `_get_user_documents` on profile |
| 19 | **Delete the Schedule page** | ✅ | No Schedule page/route exists in nav — nothing to delete |
| 20 | Sidebar: Interpreter Invoices → employees; Client Invoices → clients | ✅ | portal_base.html:33-54 |
| 21 | Sidebar: Job Offers + Availability → employees only | ✅ | portal_base.html:42-52 |
| 22 | Calendar: each interpreter sees only their confirmed; click date → details | ✅ | `portal_calendar.html` cal-event → `/portal/assignments/{id}` |
| 23 | **Admin Dashboard** (admin only) | ✅ | portal_pages.py:72-103 |
| 24 | **Interpreter Dashboard**: upcoming + button to Interpreter Invoices; employees only | ✅ | portal_pages.py:106-133 |
| 25 | Interpreter Dashboard also shows job offers + calendar | ✅ | `offers` passed to dashboard (portal_pages.py:115) |
| 26 | **Client Dashboard**: request summary, upcoming, client invoices | ✅ | portal_pages.py:136-163 |
| 27 | On assignment creation, also create client invoice auto-filled | ✅ | `ensure_invoice_for_job` portal_jobs.py:339-340 |
| 28 | Employee sidebar order: Dashboard·Job Offers·Assignments·Interpreter Invoices·Availability·Documents·Profile | ✅ | portal_base.html:40-54 (extra "Submit for Payment" item present) |
| 29 | **Requests page** (clients only) from RoseSLI.com request form | ✅ | `/portal/requests` + portal_requests.html |
| 30 | Client sidebar order: Dashboard·Requests·Client Invoice·Documents·Profile | ✅ | portal_base.html:33-38 |

## Outstanding work
1. **#15 (totals model)** — ONE decision left: should a selected differential change the
   *main invoice line* total to `(rate+diff)×duration`? Today extra differential lines add
   ON TOP of the base `duration×rate`, so wiring the differential into the base total is a
   billing-math change. Auto-SELECT of the differential is already shipped; the math wiring waits on this call.

## Open clarifications (only #3 / #15-math remain)
- **#3 "sends all information to payment"**: no payment processor is wired in this repo.
  Current behavior = invoice flips to `submitted_for_payment` + coordinators emailed + it now
  appears on the new **Interpreter Review** page (#1). If "payment" means an external system
  (Stripe / QuickBooks / bookkeeper handoff), say which — otherwise the in-portal review flow is complete.

## Uncommitted work
- `portal_offers.py`, `portal_email.py` — Gap 1 (withdraw email) + confirm email, done, **not yet committed**.

## Progress log
- 2026-06-20: Checklist created after scope was lost to summarization.
- 2026-06-20: Full audit complete. 26/30 verified shipped. Outstanding: #12, #15. Clarify: #1, #3.
- 2026-06-21: Built #1 (Interpreter Review admin page), #12 (admin Job Offers grouped by status),
  and #15 auto-SELECT of differential. 130 pass / 2 fail (known intentional MFA reds).
- 2026-06-21: SHIPPED — commit bfaa0fb pushed to main; Cloud Run rev rosesli-00118-czc 100% traffic;
  prod verified. Remaining: #15 totals-math decision; #3 only if external payment system intended.
