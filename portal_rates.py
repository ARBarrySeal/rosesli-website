"""Effective-dated rates and differentials (Rose SLI).

Rate changes apply by SERVICE DATE (locked decision 2026-07-09): future-dated
work resolves to the new rate even if already booked; records dated before the
effective date are frozen. `portal_users.interpreter_rate` stays synced to the
currently-effective rate so existing autocomplete APIs keep working.
"""
from datetime import date, time, timedelta

import portal_db


def rate_for(user_id, service_date=None):
    """Rate effective on service_date (latest history row <= date); falls back
    to portal_users.interpreter_rate when there is no history. Returns float
    or None."""
    if user_id is None:
        return None
    if service_date:
        row = portal_db.query_one(
            "SELECT rate FROM rate_history "
            "WHERE user_id = %s AND effective_date <= %s "
            "ORDER BY effective_date DESC LIMIT 1",
            (user_id, service_date),
        )
        if row:
            return float(row["rate"])
    row = portal_db.query_one(
        "SELECT interpreter_rate FROM portal_users WHERE id = %s", (user_id,),
    )
    if row and row["interpreter_rate"] is not None:
        return float(row["interpreter_rate"])
    return None


def rate_history_for(user_id):
    """All rate rows for a user, newest effective date first."""
    return portal_db.query_all(
        "SELECT id, rate, effective_date, created_at FROM rate_history "
        "WHERE user_id = %s ORDER BY effective_date DESC",
        (user_id,),
    )


def differentials_for(company, service_date=None, include_specialty=False):
    """Active differential rows effective on service_date: per code, the latest
    row with effective_date <= date, in sort_order. specialty_* rows (per-hour
    surcharges) are included only when include_specialty is set — they sort
    after the time-band rows so dropdown positions 0-5 stay stable."""
    rows = portal_db.query_all(
        "SELECT DISTINCT ON (code) code, label, amount, sort_order "
        "FROM differentials "
        "WHERE company = %s AND active = TRUE AND effective_date <= %s "
        "ORDER BY code, effective_date DESC",
        (company, service_date or date.today()),
    )
    rows = sorted(rows, key=lambda r: (r["sort_order"], r["code"]))
    if not include_specialty:
        rows = [r for r in rows if not r["code"].startswith("specialty_")]
    return rows


def set_rate(company, user_id, rate, effective_date, created_by=None):
    """Record an effective-dated rate and apply the locked retro-recalc rule:

      * non-completed jobs for this CLIENT with event_date >= effective_date
        get client_rate updated
      * UNPAID client invoices with date_of_service >= effective_date get
        rate_per_hour + total recomputed
      * UNPAID interpreter invoices with date_of_service >= effective_date get
        base_rate / rate_applied / amount recomputed
      * paid / submitted invoices and completed jobs are never touched

    Upserts on (user_id, effective_date), syncs portal_users.interpreter_rate
    to the currently-effective rate, and returns a summary dict of row counts.
    """
    rate = round(float(rate), 2)
    urow = portal_db.query_one(
        "SELECT role FROM portal_users WHERE id = %s", (user_id,),
    )
    role = urow["role"] if urow else None

    # 'differential' stores the form value: a numeric add-on or the 'xcl'
    # no-charge sentinel — cast only when it actually looks like a number.
    diff_num = ("CASE WHEN differential ~ '^[0-9]+(\\.[0-9]+)?$' "
                "THEN differential::numeric ELSE 0 END")

    summary = {"jobs": 0, "client_invoices": 0, "interpreter_invoices": 0}

    with portal_db.transaction() as cur:
        cur.execute(
            "INSERT INTO rate_history (company, user_id, rate, effective_date, created_by) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, effective_date) DO UPDATE SET rate = EXCLUDED.rate, "
            "  created_by = EXCLUDED.created_by, created_at = NOW()",
            (company, user_id, rate, effective_date, created_by),
        )

        # Keep the legacy column pointing at today's effective rate.
        cur.execute(
            "UPDATE portal_users SET interpreter_rate = COALESCE(( "
            "  SELECT rh.rate FROM rate_history rh "
            "  WHERE rh.user_id = %s AND rh.effective_date <= %s "
            "  ORDER BY rh.effective_date DESC LIMIT 1), interpreter_rate) "
            "WHERE id = %s",
            (user_id, date.today(), user_id),
        )

        if role == "client":
            cur.execute(
                "UPDATE jobs SET client_rate = %s "
                "WHERE company = %s AND client_id = %s AND event_date >= %s "
                "  AND status != 'completed'",
                (rate, company, user_id, effective_date),
            )
            summary["jobs"] = cur.rowcount
            cur.execute(
                "UPDATE client_invoices SET rate_per_hour = %s, "
                "  total = CASE WHEN duration_hours IS NOT NULL THEN "
                "    ROUND(duration_hours * %s + COALESCE(incidentals, 0) + COALESCE(( "
                "      SELECT SUM((li->>'amount')::numeric) "
                "      FROM jsonb_array_elements(line_items::jsonb) li), 0), 2) "
                "    ELSE total END "
                "WHERE company = %s AND client_id = %s AND status = 'unpaid' "
                "  AND date_of_service >= %s",
                (rate, rate, company, user_id, effective_date),
            )
            summary["client_invoices"] = cur.rowcount
        elif role == "employee":
            cur.execute(
                f"UPDATE invoices SET base_rate = %s, "
                f"  rate_applied = %s + {diff_num}, "
                f"  amount = CASE WHEN duration_hours IS NOT NULL THEN "
                f"    ROUND(duration_hours * (%s + {diff_num}) "
                "      + COALESCE(( "
                "        SELECT SUM((li->>'amount')::numeric) "
                "        FROM jsonb_array_elements(interpreter_rates::jsonb) li), 0), 2) "
                "    ELSE amount END "
                "WHERE user_id = %s AND status = 'unpaid' "
                "  AND COALESCE(submitted, FALSE) = FALSE "
                "  AND date_of_service >= %s",
                (rate, rate, rate, user_id, effective_date),
            )
            summary["interpreter_invoices"] = cur.rowcount

    return summary


# ── Time-band differential splitting (Phase 1, 2026-07-22 batch) ───────────
# The six time-band differential codes from migrations/015: each is
# (weekday|weekend) x (day 7a-5p | evening 5p-10p | overnight 10p-7a).
# Non-time-derivable differentials (holiday, lmr, conference, specialty_*)
# are NOT computed here — they stay manual "+ Add Line" additions on the
# invoice form, same as today.
_MIN_BILLABLE_HOURS = 2.0
_BAND_BOUNDARIES_MIN = (0, 7 * 60, 17 * 60, 22 * 60, 24 * 60)  # midnight,7a,5p,10p,midnight


def _band_for_minute_of_day(minute_of_day, is_weekend):
    # minute_of_day in [0, 1440); overnight wraps both [0,7a) and [10p,24:00)
    if minute_of_day < 7 * 60 or minute_of_day >= 22 * 60:
        tod = "overnight"
    elif minute_of_day < 17 * 60:
        tod = "day"
    else:
        tod = "evening"

    if is_weekend:
        return "weekend_" + tod
    if tod == "evening":
        return "weekday_evening"
    return tod  # "day" and "overnight" have no weekday_ prefix in the table


def compute_time_band_hours(event_date, start_time, end_time):
    """Split a shift into hours per time-band differential code, splitting
    proportionally at every 7a/5p/10p/midnight boundary it crosses. Midnight
    crossings can also flip weekday->weekend (or vice versa), which changes
    the code for the portion after midnight. If the shift's actual duration
    is under the 2-hour minimum, every band's hours are scaled up so the
    total equals exactly 2.0 (proportional to the actual split), matching
    the existing 2-hour-minimum billing rule."""
    if start_time == end_time:
        raise ValueError("start_time and end_time must differ")

    start_min = start_time.hour * 60 + start_time.minute
    end_min = end_time.hour * 60 + end_time.minute
    if end_min <= start_min:
        end_min += 24 * 60  # crosses midnight

    # Boundaries: every 7a/5p/10p/midnight instant between start and end.
    boundaries = {start_min, end_min}
    day_offset = 0
    while day_offset * 1440 < end_min:
        for b in _BAND_BOUNDARIES_MIN:
            point = day_offset * 1440 + b
            if start_min < point < end_min:
                boundaries.add(point)
        day_offset += 1
    points = sorted(boundaries)

    totals = {}
    for i in range(len(points) - 1):
        seg_start, seg_end = points[i], points[i + 1]
        mid = (seg_start + seg_end) / 2.0
        day_num = int(mid // 1440)  # 0 = event_date, 1 = event_date + 1
        minute_of_day = mid % 1440
        seg_date = event_date + timedelta(days=day_num)
        is_weekend = seg_date.weekday() >= 5  # Sat=5, Sun=6
        code = _band_for_minute_of_day(minute_of_day, is_weekend)
        hours = (seg_end - seg_start) / 60.0
        totals[code] = totals.get(code, 0.0) + hours

    actual_total = sum(totals.values())
    if 0 < actual_total < _MIN_BILLABLE_HOURS:
        scale = _MIN_BILLABLE_HOURS / actual_total
        totals = {k: v * scale for k, v in totals.items()}

    return {k: round(v, 2) for k, v in totals.items()}
