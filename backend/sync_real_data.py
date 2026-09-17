"""
Sync real data from Cloudbeds into daily_metrics + reservations.
Pulls last 90 days of getDashboard + getTransactions for each branch.
Run: python sync_real_data.py
"""
import os, urllib.request, json, time, psycopg2
from datetime import date, timedelta

DB = dict(
    host=os.environ["HID_DB_HOST"],
    port=int(os.environ.get("HID_DB_PORT", "5432")),
    dbname=os.environ.get("HID_DB_NAME", "postgres"),
    user=os.environ["HID_DB_USER"],
    password=os.environ["HID_DB_PASSWORD"],
    sslmode="require",
)
def _key(property_id):
    """Cloudbeds token for one property, from the environment.

    These were hardcoded here, in a public repo, from the initial commit until
    2026-09-17. Never put one back in this file — export
    CLOUDBEDS_KEY_<property_id> before running.
    """
    var = f"CLOUDBEDS_KEY_{property_id}"
    try:
        return os.environ[var]
    except KeyError:
        raise SystemExit(f"Missing {var} in the environment")

BASE = "https://hotels.cloudbeds.com/api/v1.2"

BRANCHES = [
    ("11111111-1111-1111-1111-111111111101", "MEANDER Taipei",  "25496", "TWD"),
    ("11111111-1111-1111-1111-111111111102", "MEANDER Saigon",  "185944", "VND"),
    ("11111111-1111-1111-1111-111111111103", "MEANDER 1948",    "22872",  "VND"),
    ("11111111-1111-1111-1111-111111111104", "MEANDER Oani",    "318301", "VND"),
    ("11111111-1111-1111-1111-111111111105", "MEANDER Osaka",   "301582", "JPY"),
]
DAYS_BACK = 90


def api_get(path, params, key, timeout=20):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}/{path}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_dashboard(prop_id, key, day):
    try:
        d = api_get("getDashboard", {"propertyID": prop_id, "startDate": day, "endDate": day}, key)
        if d.get("success"):
            return d["data"]
    except Exception as e:
        print(f"    getDashboard {day} err: {e}")
    return None


def get_daily_revenue(prop_id, key, day):
    """Sum room-charge transactions for a given serviceDate."""
    try:
        d = api_get("getTransactions", {
            "propertyID": prop_id,
            "startDate": day, "endDate": day,
            "transactionCategory": "charge",
            "pageSize": 500,
        }, key, timeout=30)
        if not d.get("success"):
            return None
        total = sum(
            float(t.get("amount", 0))
            for t in d.get("data", [])
            if not t.get("isDeleted")
               and "room" in (t.get("itemCategoryName") or "").lower()
        )
        return total if total > 0 else None
    except Exception as e:
        print(f"    getTransactions {day} err: {e}")
        return None


conn = psycopg2.connect(**DB)
conn.autocommit = False
cur = conn.cursor()

today = date.today()

for branch_id, name, prop_id, currency in BRANCHES:
    api_key = _key(prop_id)
    print(f"\n[{name}] syncing {DAYS_BACK} days...")
    ok_days = 0
    for i in range(DAYS_BACK - 1, -1, -1):
        day = today - timedelta(days=i)
        dash = get_dashboard(prop_id, api_key, day.isoformat())
        if not dash:
            continue

        capacity   = int(dash.get("capacity", 0))
        rooms_sold = int(dash.get("roomsOccupied", 0))
        occ_pct    = float(dash.get("percentageOccupied", 0)) / 100.0
        cancels    = int(dash.get("cancellations", 0))
        new_bk     = int(dash.get("bookings", 0))

        revenue = get_daily_revenue(prop_id, api_key, day.isoformat())
        adr     = round(revenue / rooms_sold, 2) if revenue and rooms_sold > 0 else None
        revpar  = round(revenue / capacity, 2)   if revenue and capacity > 0  else None

        cur.execute("""
            INSERT INTO daily_metrics
              (branch_id, date, rooms_sold, total_sold, occ_pct, room_occ_pct,
               revenue_native, revenue_vnd, adr_native, revpar_native,
               new_bookings, cancellations, cancellation_pct)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (branch_id, date) DO UPDATE SET
              rooms_sold=EXCLUDED.rooms_sold,
              total_sold=EXCLUDED.total_sold,
              occ_pct=EXCLUDED.occ_pct,
              room_occ_pct=EXCLUDED.room_occ_pct,
              revenue_native=COALESCE(EXCLUDED.revenue_native, daily_metrics.revenue_native),
              revenue_vnd=COALESCE(EXCLUDED.revenue_vnd, daily_metrics.revenue_vnd),
              adr_native=COALESCE(EXCLUDED.adr_native, daily_metrics.adr_native),
              revpar_native=COALESCE(EXCLUDED.revpar_native, daily_metrics.revpar_native),
              new_bookings=EXCLUDED.new_bookings,
              cancellations=EXCLUDED.cancellations,
              cancellation_pct=EXCLUDED.cancellation_pct
        """, (
            branch_id, day,
            rooms_sold, rooms_sold,
            round(occ_pct, 4), round(occ_pct, 4),
            revenue, revenue,
            adr, revpar,
            new_bk, cancels,
            round(cancels / (new_bk + cancels), 4) if (new_bk + cancels) > 0 else 0,
        ))
        ok_days += 1
        if ok_days % 10 == 0:
            conn.commit()
            print(f"  {ok_days}/{DAYS_BACK} days done...")
        time.sleep(0.15)  # rate limiting

    conn.commit()
    print(f"  Done: {ok_days} days synced for {name}")

cur.close()
conn.close()
print("\nAll done! Real data synced.")
