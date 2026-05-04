"""Insights router — KOL-to-Paid Ads opportunities"""
import re
from datetime import datetime, date, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.kol import KOLRecord

_KOL_RE = re.compile(r"\(KOL_([^)]+)\)")

# Maps common nationality adjectives → country name fragment for matching
_NAT_MAP = {
    "vietnamese": "vietnam",
    "korean": "korea",
    "japanese": "japan",
    "taiwanese": "taiwan",
    "chinese": "china",
    "indonesian": "indonesia",
    "thai": "thailand",
    "singaporean": "singapore",
    "malaysian": "malaysia",
    "filipino": "philippines",
    "philippine": "philippines",
    "australian": "australia",
    "american": "united states",
    "french": "france",
    "german": "germany",
    "british": "united kingdom",
    "canadian": "canada",
    "spanish": "spain",
    "italian": "italy",
    "dutch": "netherlands",
    "russian": "russia",
    "indian": "india",
    "hongkonger": "hong kong",
    "hong konger": "hong kong",
    "hong kong": "hong kong",
    "swedish": "sweden",
    "norwegian": "norway",
    "danish": "denmark",
    "finnish": "finland",
    "polish": "poland",
    "swiss": "switzerland",
    "austrian": "austria",
    "belgian": "belgium",
    "portuguese": "portugal",
    "greek": "greece",
    "turkish": "turkey",
    "saudi": "saudi arabia",
    "emirati": "united arab emirates",
    "uae": "united arab emirates",
    "new zealander": "new zealand",
    "kiwi": "new zealand",
    "south african": "south africa",
    "mexican": "mexico",
    "brazilian": "brazil",
    "argentinian": "argentina",
    "chilean": "chile",
    "colombian": "colombia",
}


def _nat_matches_country(nationality: str, country: str) -> bool:
    nat = nationality.lower().strip()
    cty = country.lower().strip()
    mapped = _NAT_MAP.get(nat, nat)
    return mapped in cty or cty in mapped or nat in cty or cty in nat

router = APIRouter()


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("")
def list_insights(
    branch_id: Optional[UUID] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """
    Returns KOL records that are eligible for paid ads (paid_ads_eligible=True)
    but have no paid_ads_channel set yet — i.e. untapped opportunities.
    Also flags records with expiring usage rights.
    """
    today = date.today()
    q = db.query(KOLRecord).filter(KOLRecord.paid_ads_eligible == True)
    if branch_id:
        q = q.filter(KOLRecord.branch_id == branch_id)
    rows = q.order_by(KOLRecord.published_date.desc().nullslast()).limit(limit).all()

    result = []
    for r in rows:
        expiry_days = None
        if r.usage_rights_expiry_date:
            expiry_days = (r.usage_rights_expiry_date - today).days

        opportunity_type = []
        if not r.paid_ads_channel:
            opportunity_type.append("No channel set")
        if expiry_days is not None and 0 < expiry_days <= 30:
            opportunity_type.append(f"Rights expire in {expiry_days}d")
        if expiry_days is not None and expiry_days <= 0:
            opportunity_type.append("Rights expired")

        result.append({
            "id": str(r.id),
            "branch_id": str(r.branch_id),
            "kol_name": r.kol_name,
            "kol_nationality": r.kol_nationality,
            "language": r.language,
            "target_audience": r.target_audience,
            "published_date": r.published_date.isoformat() if r.published_date else None,
            "link_ig": r.link_ig,
            "link_tiktok": r.link_tiktok,
            "link_youtube": r.link_youtube,
            "deliverable_status": r.deliverable_status,
            "paid_ads_channel": r.paid_ads_channel,
            "paid_ads_usage_fee_vnd": float(r.paid_ads_usage_fee_vnd) if r.paid_ads_usage_fee_vnd else None,
            "usage_rights_expiry_date": r.usage_rights_expiry_date.isoformat() if r.usage_rights_expiry_date else None,
            "expiry_days_left": expiry_days,
            "opportunity_type": opportunity_type,
            "is_actionable": len(opportunity_type) > 0,
        })

    # Sort: rights expiring soon first, then unpublished
    result.sort(key=lambda x: (
        0 if (x["expiry_days_left"] is not None and 0 < x["expiry_days_left"] <= 30) else 1,
        x["expiry_days_left"] if x["expiry_days_left"] is not None else 9999,
    ))
    sync_q = db.query(func.max(KOLRecord.updated_at))
    if branch_id:
        sync_q = sync_q.filter(KOLRecord.branch_id == branch_id)
    sync_ts = sync_q.scalar()
    last_synced_iso = sync_ts.isoformat() if sync_ts else None
    for row in result:
        row["data_synced_at"] = last_synced_iso
    return _envelope(result)


@router.get("/country-intel")
def country_intelligence(
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Returns two ranked lists per branch:
    - top_volume:  Top 5 countries by booking count (last 30 days)
    - top_growth:  Top 5 countries by 30-day booking growth vs prior 30 days
    Cross-referenced with KOL coverage, Paid Ads coverage, and government visitor data.
    """
    b_filter = "AND r.branch_id = :bid" if branch_id else ""
    b_params = {"bid": str(branch_id)} if branch_id else {}

    # ── 1. Top 5 by booking volume (last 30 days) ───────────────────────────
    country_rows = db.execute(text(f"""
        WITH ranked AS (
            SELECT
                b.id           AS branch_id,
                b.name         AS branch_name,
                b.currency,
                r.guest_country,
                r.guest_country_code,
                COUNT(*)                                          AS booking_count,
                SUM(r.grand_total_vnd)                           AS revenue_vnd,
                ROW_NUMBER() OVER (
                    PARTITION BY b.id
                    ORDER BY COUNT(*) DESC,
                        COALESCE(SUM(r.grand_total_vnd), 0) DESC
                )                                                AS rank
            FROM reservations r
            JOIN branches b ON r.branch_id = b.id
            WHERE r.guest_country IS NOT NULL
              AND r.guest_country != ''
              AND r.guest_country != '0'
              AND length(r.guest_country) > 1
              AND r.status NOT IN (
                  'canceled','cancelled','no_show','no-show','cancelled_by_guest'
              )
              AND r.check_in_date >= CURRENT_DATE - INTERVAL '30 days'
              {b_filter}
            GROUP BY b.id, b.name, b.currency, r.guest_country, r.guest_country_code
        )
        SELECT branch_id, branch_name, currency, guest_country, guest_country_code,
               booking_count, revenue_vnd, rank
        FROM ranked
        WHERE rank <= 5
        ORDER BY branch_name, rank
    """), b_params).fetchall()

    # ── 2. Top 5 by growth (recent 30d vs prior 30d) ─────────────────────────
    growth_rows = db.execute(text(f"""
        WITH recent AS (
            SELECT r.branch_id, r.guest_country, r.guest_country_code, COUNT(*) AS cnt
            FROM reservations r
            WHERE r.guest_country IS NOT NULL
              AND r.guest_country != ''
              AND r.guest_country != '0'
              AND length(r.guest_country) > 1
              AND r.status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')
              AND r.check_in_date >= CURRENT_DATE - INTERVAL '30 days'
              AND r.guest_country NOT ILIKE '%unknown%'
              {b_filter}
            GROUP BY r.branch_id, r.guest_country, r.guest_country_code
        ),
        prev AS (
            SELECT r.branch_id, r.guest_country, COUNT(*) AS cnt
            FROM reservations r
            WHERE r.guest_country IS NOT NULL
              AND r.guest_country != ''
              AND r.guest_country != '0'
              AND length(r.guest_country) > 1
              AND r.status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')
              AND r.check_in_date >= CURRENT_DATE - INTERVAL '60 days'
              AND r.check_in_date < CURRENT_DATE - INTERVAL '30 days'
              AND r.guest_country NOT ILIKE '%unknown%'
              {b_filter}
            GROUP BY r.branch_id, r.guest_country
        ),
        growth AS (
            SELECT
                rec.branch_id,
                rec.guest_country,
                rec.guest_country_code,
                rec.cnt                                          AS recent_bookings,
                COALESCE(prv.cnt, 0)                             AS prev_bookings,
                CASE
                    WHEN COALESCE(prv.cnt, 0) = 0 THEN NULL
                    ELSE ROUND(((rec.cnt - prv.cnt)::numeric / prv.cnt * 100), 1)
                END                                              AS growth_pct,
                ROW_NUMBER() OVER (
                    PARTITION BY rec.branch_id
                    ORDER BY
                        CASE WHEN COALESCE(prv.cnt, 0) > 0 AND rec.cnt > prv.cnt
                             THEN (rec.cnt - prv.cnt)::float / prv.cnt ELSE -1 END DESC
                )                                                AS rank
            FROM recent rec
            LEFT JOIN prev prv
                   ON rec.branch_id = prv.branch_id AND rec.guest_country = prv.guest_country
            WHERE rec.cnt >= 5      -- require ≥5 bookings in recent 30d to qualify as a trend
              AND COALESCE(prv.cnt, 0) >= 2  -- and ≥2 in prior 30d (genuine growth, not noise)
        )
        SELECT g.branch_id, b.name AS branch_name, b.currency,
               g.guest_country, g.guest_country_code,
               g.recent_bookings, g.prev_bookings, g.growth_pct, g.rank
        FROM growth g
        JOIN branches b ON g.branch_id = b.id
        WHERE g.rank <= 5
          AND (g.growth_pct > 0 OR g.prev_bookings = 0)
        ORDER BY b.name, g.rank
    """), b_params).fetchall()

    if not country_rows and not growth_rows:
        return _envelope([])

    # ── 2. All KOL records with nationality ──────────────────────────────────
    kol_rows = db.execute(text("""
        SELECT kol_name, kol_nationality, language, target_audience,
               deliverable_status, contract_status, link_ig, link_tiktok, link_youtube,
               branch_id
        FROM kol_records
        WHERE kol_nationality IS NOT NULL AND kol_nationality != ''
    """)).fetchall()

    # ── 3. KOL organic metrics from reservations ─────────────────────────────
    kol_res = db.execute(text("""
        SELECT room_type, branch_id, grand_total_vnd, status
        FROM reservations
        WHERE room_type ILIKE '%KOL_%'
          AND status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')
    """)).fetchall()

    kol_metric_map: dict[str, dict] = {}
    for row in kol_res:
        m = _KOL_RE.search(row[0] or "")
        if not m:
            continue
        kol_name = "KOL_" + m.group(1).strip()
        key = kol_name
        if key not in kol_metric_map:
            kol_metric_map[key] = {"organic_bookings": 0, "organic_revenue_vnd": 0.0}
        kol_metric_map[key]["organic_bookings"] += 1
        kol_metric_map[key]["organic_revenue_vnd"] += float(row[2] or 0)

    # ── 4. Ads coverage per (branch_id, target_country, channel) ────────────
    #   Also fetch MAX(date_to) to determine Running vs Stopped.
    #   "Running" = MAX(date_to) within last 7 days.  "Stopped" = older.
    a_filter = "AND a.branch_id = :bid" if branch_id else ""
    ads_rows = db.execute(text(f"""
        SELECT
            a.branch_id,
            a.target_country,
            a.channel,
            SUM(a.cost_native)      AS total_cost_native,
            SUM(a.revenue_native)   AS total_revenue_native,
            SUM(a.impressions)      AS total_impressions,
            SUM(a.clicks)           AS total_clicks,
            SUM(a.leads)            AS total_leads,
            SUM(a.bookings)         AS total_bookings,
            MAX(a.date_to)          AS last_active_date
        FROM ads_performance a
        WHERE a.target_country IS NOT NULL AND a.target_country != ''
          {a_filter}
        GROUP BY a.branch_id, a.target_country, a.channel
        ORDER BY a.branch_id, a.target_country, a.channel
    """), b_params).fetchall()

    from datetime import date, timedelta
    running_cutoff = date.today() - timedelta(days=2)

    # ads_map: branch_id -> { country_lower -> { target_country, channels, has_running } }
    ads_map: dict[str, dict] = {}
    for a in ads_rows:
        bid_str = str(a[0])
        country = a[1] or ""
        ckey = country.lower()
        cost = float(a[3] or 0)
        rev  = float(a[4] or 0)
        last_date = a[9]  # MAX(date_to) — could be date or None
        is_running = bool(last_date and last_date >= running_cutoff)
        if bid_str not in ads_map:
            ads_map[bid_str] = {}
        if ckey not in ads_map[bid_str]:
            ads_map[bid_str][ckey] = {
                "target_country": country,
                "country_lower": ckey,
                "channels": [],
                "has_running": False,
            }
        if is_running:
            ads_map[bid_str][ckey]["has_running"] = True
        ads_map[bid_str][ckey]["channels"].append({
            "channel": a[2] or "Unknown",
            "total_cost_native": cost,
            "total_revenue_native": rev,
            "roas": round(rev / cost, 2) if cost > 0 else None,
            "total_impressions": int(a[5] or 0),
            "total_clicks": int(a[6] or 0),
            "total_leads": int(a[7] or 0),
            "total_bookings": int(a[8] or 0),
            "status": "running" if is_running else "stopped",
            "last_active_date": str(last_date) if last_date else None,
        })

    # ── 4b. Room type stats per (branch, country) ─────────────────────────
    rt_rows = db.execute(text(f"""
        SELECT r.branch_id, r.guest_country, r.room_type, r.adults,
               COUNT(*) AS cnt
        FROM reservations r
        WHERE r.guest_country IS NOT NULL
          AND r.guest_country != ''
          AND r.guest_country != '0'
          AND length(r.guest_country) > 1
          AND r.status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')
          AND r.check_in_date >= CURRENT_DATE - INTERVAL '90 days'
          {b_filter}
        GROUP BY r.branch_id, r.guest_country, r.room_type, r.adults
        ORDER BY r.branch_id, r.guest_country, cnt DESC
    """), b_params).fetchall()

    # rt_map: branch_id -> { country_lower -> { adults_count -> { room_type -> cnt } } }
    rt_map: dict[str, dict] = {}
    rt_totals: dict[str, dict] = {}  # branch_id -> { country_lower -> total }
    for row in rt_rows:
        bid_s = str(row[0])
        gc = (row[1] or "").lower()
        rt = row[2] or "Unknown"
        adults = int(row[3]) if row[3] is not None else 0
        cnt = int(row[4])
        rt_map.setdefault(bid_s, {}).setdefault(gc, {}).setdefault(adults, {})
        rt_map[bid_s][gc][adults][rt] = rt_map[bid_s][gc][adults].get(rt, 0) + cnt
        rt_totals.setdefault(bid_s, {}).setdefault(gc, 0)
        rt_totals[bid_s][gc] += cnt

    def _get_room_type_stats(bid_str, country):
        gc = country.lower()
        adults_data = rt_map.get(bid_str, {}).get(gc, {})
        if not adults_data:
            return []
        total_bookings = rt_totals.get(bid_str, {}).get(gc, 0)
        # Build segments sorted by total bookings desc
        segments = []
        for adults_count, room_types in sorted(adults_data.items(), key=lambda x: sum(x[1].values()), reverse=True):
            segment_total = sum(room_types.values())
            pct = round(segment_total / total_bookings * 100, 1) if total_bookings > 0 else 0
            top_rooms = sorted(room_types.items(), key=lambda x: x[1], reverse=True)[:3]
            segments.append({
                "adults": adults_count,
                "booking_count": segment_total,
                "pct": pct,
                "top_rooms": [
                    {"room_type": rt, "count": c, "pct": round(c / segment_total * 100, 1) if segment_total > 0 else 0}
                    for rt, c in top_rooms
                ],
            })
        return segments

    # ── 5. Helper: build a country entry with KOL/Ads coverage ──────────────
    def _build_country(bid_str, country, country_code):
        cty_lower = country.lower()
        matched_kols = []
        for k in kol_rows:
            if str(k[9]) != bid_str:
                continue
            if k[1] and _nat_matches_country(k[1], country):
                metrics = kol_metric_map.get(k[0], {})
                matched_kols.append({
                    "kol_name": k[0],
                    "kol_nationality": k[1],
                    "language": k[2],
                    "target_audience": k[3],
                    "status": k[4] or k[5],
                    "link_ig": k[6],
                    "link_tiktok": k[7],
                    "link_youtube": k[8],
                    "organic_bookings": metrics.get("organic_bookings", 0),
                    "organic_revenue_vnd": metrics.get("organic_revenue_vnd", 0.0),
                })
        matched_ads = []
        any_running = False
        country_ads_dict = ads_map.get(bid_str, {})
        for ckey, ad in country_ads_dict.items():
            if ckey in cty_lower or cty_lower in ckey:
                matched_ads.append({k: v for k, v in ad.items() if k not in ("country_lower", "has_running")})
                if ad.get("has_running"):
                    any_running = True

        # Ads status: "running" | "stopped" | "none"
        #   running  = at least 1 channel active in last 7 days → Covered
        #   stopped  = had ads but all stopped → NOT covered (gap)
        #   none     = never had ads → NOT covered (gap)
        if matched_ads and any_running:
            ads_status = "running"
        elif matched_ads:
            ads_status = "stopped"
        else:
            ads_status = "none"

        action_items = []
        if not matched_kols:
            action_items.append(f"No KOL from {country} — identify & recruit")
        if ads_status == "none":
            action_items.append(f"No Paid Ads targeting {country} — create campaign")
        elif ads_status == "stopped":
            action_items.append(f"Paid Ads targeting {country} stopped — consider restarting")

        return {
            "country": country,
            "country_code": country_code,
            "kol_coverage": matched_kols,
            "ads_coverage": matched_ads,
            "ads_status": ads_status,
            "kol_gap": len(matched_kols) == 0,
            "ads_gap": ads_status != "running",  # Only "running" = covered
            "action_items": action_items,
            "room_type_stats": _get_room_type_stats(bid_str, country),
        }

    # ── 5b. Government visitor data ───────────────────────────────────────────
    gov_rows = db.execute(text("""
        SELECT destination, source_country, rank,
               jan, feb, mar, apr, may, jun, jul, aug, sep, oct, nov, dec, total
        FROM gov_visitor_data
        ORDER BY destination, rank
    """)).fetchall()

    # gov_map: { destination_lower -> { source_country_lower -> row_dict } }
    gov_map: dict[str, dict] = {}
    for g in gov_rows:
        dest = (g[0] or "").lower()
        src = (g[1] or "").lower()
        if dest not in gov_map:
            gov_map[dest] = {}
        gov_map[dest][src] = {
            "destination": g[0],
            "source_country": g[1],
            "rank": g[2],
            "jan": g[3] or 0, "feb": g[4] or 0, "mar": g[5] or 0,
            "apr": g[6] or 0, "may": g[7] or 0, "jun": g[8] or 0,
            "jul": g[9] or 0, "aug": g[10] or 0, "sep": g[11] or 0,
            "oct": g[12] or 0, "nov": g[13] or 0, "dec": g[14] or 0,
            "total": g[15] or 0,
        }

    # Map branch country to gov destination
    _BRANCH_DEST_MAP = {
        "taiwan": "Taiwan",
        "japan": "Japan",
        "vietnam": "Vietnam",
        "viet nam": "Vietnam",
    }

    # Fallback: map branch NAME to gov destination (when branch.country is empty)
    # Saigon → Vietnam, Taipei/1948/Oani → Taiwan, Osaka → Japan
    _BRANCH_NAME_DEST_MAP = {
        "meander taipei": "Taiwan",
        "meander 1948": "Taiwan",
        "meander oani": "Taiwan",
        "meander osaka": "Japan",
        "meander saigon": "Vietnam",
    }

    def _resolve_dest(branch_id: str, branch_country: str = None, branch_name: str = None) -> str:
        """Resolve branch to gov destination key. Tries branch.country first, then branch name."""
        bc = (branch_country or "").lower().strip()
        if bc and bc in _BRANCH_DEST_MAP:
            return _BRANCH_DEST_MAP[bc]
        # Fallback: use branch name
        bn = (branch_name or "").lower().strip()
        for key, dest in _BRANCH_NAME_DEST_MAP.items():
            if key in bn or bn in key:
                return dest
        return bc or bn

    def _find_gov_data(branch_country: str, guest_country: str, branch_name: str = None):
        """Find government visitor data matching branch destination + guest source country."""
        dest_key = _resolve_dest("", branch_country, branch_name)
        dest_data = gov_map.get(dest_key.lower(), {})
        gc = guest_country.lower()
        # Direct match
        if gc in dest_data:
            return dest_data[gc]
        # Partial match
        for key, val in dest_data.items():
            if gc in key or key in gc:
                return val
        return None

    # ── 6. Build response ────────────────────────────────────────────────────
    branch_map: dict[str, dict] = {}

    # Fetch branch country + name for gov data mapping
    branch_info = {}
    branch_name_map = {}
    branch_info_rows = db.execute(text("SELECT id, country, name FROM branches")).fetchall()
    for bi in branch_info_rows:
        branch_info[str(bi[0])] = bi[1] or ""
        branch_name_map[str(bi[0])] = bi[2] or ""

    for r in country_rows:
        bid_str = str(r[0])
        if bid_str not in branch_map:
            branch_map[bid_str] = {
                "branch_id": bid_str,
                "branch_name": r[1],
                "currency": r[2],
                "top_volume": [],
                "top_growth": [],
            }
        entry = _build_country(bid_str, r[3], r[4])
        gov = _find_gov_data(branch_info.get(bid_str, ""), r[3], branch_name=branch_name_map.get(bid_str, ""))
        entry.update({
            "rank": int(r[7]),
            "booking_count": int(r[5]),
            "revenue_vnd": float(r[6]) if r[6] else 0.0,
            "gov_visitor_data": gov,
        })
        branch_map[bid_str]["top_volume"].append(entry)

    for r in growth_rows:
        bid_str = str(r[0])
        if bid_str not in branch_map:
            branch_map[bid_str] = {
                "branch_id": bid_str,
                "branch_name": r[1],
                "currency": r[2],
                "top_volume": [],
                "top_growth": [],
            }
        entry = _build_country(bid_str, r[3], r[4])
        gov = _find_gov_data(branch_info.get(bid_str, ""), r[3], branch_name=branch_name_map.get(bid_str, ""))
        entry.update({
            "rank": int(r[8]),
            "recent_bookings": int(r[5]),
            "prev_bookings": int(r[6]),
            "growth_pct": float(r[7]) if r[7] is not None else None,
            "gov_visitor_data": gov,
        })
        branch_map[bid_str]["top_growth"].append(entry)

    # ── 7. Gov visitor forecast for upcoming months ─────────────────────────
    from datetime import date as _date
    today = _date.today()
    next_month = today.month % 12 + 1
    next2_month = (today.month + 1) % 12 + 1
    month_cols = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"]

    def _build_gov_forecast_raw(month_num: int):
        """Return gov visitor data ranked by visitor count for a specific month, per branch destination."""
        col = month_cols[month_num - 1]
        forecast_rows = db.execute(text(f"""
            SELECT destination, source_country, rank, {col} AS visitor_count, total
            FROM gov_visitor_data
            WHERE {col} > 0
            ORDER BY destination, {col} DESC
        """)).fetchall()

        dest_map = {}
        for fr in forecast_rows:
            dest = fr[0]
            if dest not in dest_map:
                dest_map[dest] = []
            dest_map[dest].append({
                "source_country": fr[1],
                "gov_rank": fr[2],
                "visitor_count": int(fr[3] or 0),
                "yearly_total": int(fr[4] or 0),
            })
        return dest_map

    # Common country name aliases for matching
    _COUNTRY_ALIASES = {
        "usa": "united states",
        "u.s.a.": "united states",
        "united states of america": "united states",
        "uk": "united kingdom",
        "great britain": "united kingdom",
        "korea": "south korea",
        "republic of korea": "south korea",
        "south korea": "korea",
        "hong kong sar": "hong kong",
        "mainland china": "china",
        "p.r. china": "china",
        "uae": "united arab emirates",
    }

    def _normalize_country(name: str) -> str:
        n = name.lower().strip()
        return _COUNTRY_ALIASES.get(n, n)

    def _country_matches(name_a: str, name_b: str) -> bool:
        """Fuzzy match two country names with alias support."""
        a = name_a.lower().strip()
        b = name_b.lower().strip()
        if a == b:
            return True
        # Normalize via aliases
        na = _normalize_country(a)
        nb = _normalize_country(b)
        if na == nb:
            return True
        # Check nationality → country mapping
        mapped_a = _NAT_MAP.get(a, na)
        mapped_b = _NAT_MAP.get(b, nb)
        if mapped_a == mapped_b:
            return True
        return mapped_a in mapped_b or mapped_b in mapped_a or na in nb or nb in na

    def _get_month_bookings(month_num: int, branch_id: str):
        """
        Get top 5 countries by booking count for a specific month (by check-in date),
        plus average lead time (days between booking created and check-in).
        """
        # Determine year: if month_num <= current month, it's next year
        target_year = today.year if month_num > today.month else today.year + 1
        month_start = f"{target_year}-{month_num:02d}-01"
        if month_num == 12:
            month_end = f"{target_year + 1}-01-01"
        else:
            month_end = f"{target_year}-{month_num + 1:02d}-01"

        rows = db.execute(text("""
            SELECT
                r.guest_country,
                r.guest_country_code,
                COUNT(*)                                            AS booking_count,
                COALESCE(SUM(r.grand_total_vnd), 0)                AS revenue_vnd,
                AVG(r.check_in_date - r.reservation_date)          AS avg_lead_days,
                ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, COALESCE(SUM(r.grand_total_vnd), 0) DESC) AS rank
            FROM reservations r
            WHERE r.branch_id = :bid
              AND r.guest_country IS NOT NULL
              AND r.guest_country != ''
              AND r.guest_country != '0'
              AND length(r.guest_country) > 1
              AND r.status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')
              AND r.check_in_date >= :m_start
              AND r.check_in_date < :m_end
            GROUP BY r.guest_country, r.guest_country_code
            ORDER BY COUNT(*) DESC
            LIMIT 5
        """), {"bid": branch_id, "m_start": month_start, "m_end": month_end}).fetchall()

        result = {}
        for r in rows:
            avg_lead = None
            if r[4] is not None:
                try:
                    avg_lead = round(float(r[4]), 1)
                except (TypeError, ValueError):
                    avg_lead = None
            result[r[0].lower()] = {
                "country": r[0],
                "country_code": r[1],
                "booking_count": int(r[2]),
                "revenue_vnd": float(r[3]),
                "avg_lead_days": avg_lead,
                "rank": int(r[5]),
            }
        return result

    def _build_combined_forecast(month_num: int, branch_id: str, dest_key: str):
        """
        Cross-reference gov visitor forecast with bookings for that specific month.
        Only return countries that appear in BOTH gov data AND top bookings.
        Add reason notes + avg lead time for each match.
        """
        raw_gov = _build_gov_forecast_raw(month_num)
        gov_countries = raw_gov.get(dest_key, raw_gov.get(dest_key.capitalize(), []))

        # Get bookings for this specific month by check-in date
        volume_lookup = _get_month_bookings(month_num, branch_id)

        matched = []
        for gc in gov_countries:
            src = gc["source_country"]
            booking_info = None
            for vk, vv in volume_lookup.items():
                if _country_matches(src, vk):
                    booking_info = vv
                    break

            if booking_info:
                import calendar as _cal
                month_name = _cal.month_name[month_num]
                reasons = []
                reasons.append(f"#{booking_info['rank']} bookings in {month_name}: {booking_info['booking_count']} bookings")
                reasons.append(f"Gov forecast: #{gc['gov_rank']} source market with {gc['visitor_count']:,} visitors")
                if booking_info["avg_lead_days"] is not None:
                    reasons.append(f"Avg lead time: {booking_info['avg_lead_days']} days")
                matched.append({
                    **gc,
                    "booking_count": booking_info["booking_count"],
                    "booking_rank": booking_info["rank"],
                    "revenue_vnd": booking_info["revenue_vnd"],
                    "avg_lead_days": booking_info["avg_lead_days"],
                    "match_type": "volume",
                    "reasons": reasons,
                })

        # Sort by gov visitor count descending
        matched.sort(key=lambda x: x["visitor_count"], reverse=True)
        return matched

    gov_raw_next = _build_gov_forecast_raw(next_month)
    gov_raw_next2 = _build_gov_forecast_raw(next2_month)

    import calendar
    next_month_name = calendar.month_name[next_month]
    next2_month_name = calendar.month_name[next2_month]

    for bdata in branch_map.values():
        bid = bdata["branch_id"]
        dest_key = _resolve_dest(bid, branch_info.get(bid, ""), branch_name_map.get(bid, ""))

        combined_next = _build_combined_forecast(next_month, bid, dest_key)
        combined_next2 = _build_combined_forecast(next2_month, bid, dest_key)

        # Also keep full gov list for reference
        all_gov_next = gov_raw_next.get(dest_key, gov_raw_next.get(dest_key.capitalize(), []))[:10]
        all_gov_next2 = gov_raw_next2.get(dest_key, gov_raw_next2.get(dest_key.capitalize(), []))[:10]

        bdata["gov_forecast"] = {
            "next_month": {
                "month_num": next_month,
                "month_name": next_month_name,
                "countries": combined_next,
                "all_gov_countries": all_gov_next,
            },
            "next_2_months": {
                "month_num": next2_month,
                "month_name": next2_month_name,
                "countries": combined_next2,
                "all_gov_countries": all_gov_next2,
            },
        }

    result = sorted(branch_map.values(), key=lambda x: x["branch_name"])
    return _envelope(result)
