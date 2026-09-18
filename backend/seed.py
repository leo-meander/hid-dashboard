"""
Seed script — inserts sample data for HiD dashboard testing.
Run: python seed.py
"""
import os
import psycopg2
import random
from datetime import date, timedelta

DB = dict(
    host=os.environ["HID_DB_HOST"],
    port=int(os.environ.get("HID_DB_PORT", "5432")),
    dbname=os.environ.get("HID_DB_NAME", "postgres"),
    user=os.environ["HID_DB_USER"],
    password=os.environ["HID_DB_PASSWORD"],
    sslmode="require",
)

conn = psycopg2.connect(**DB)
conn.autocommit = False
cur = conn.cursor()

# ── 1. Branches ──────────────────────────────────────────────────────────────
print("Inserting branches...")
branches = [
    ("11111111-1111-1111-1111-111111111101", "HiD Hanoi",            "Hanoi",           "VN", "Asia/Ho_Chi_Minh"),
    ("11111111-1111-1111-1111-111111111102", "HiD Ho Chi Minh City", "Ho Chi Minh City","VN", "Asia/Ho_Chi_Minh"),
    ("11111111-1111-1111-1111-111111111103", "HiD Da Nang",          "Da Nang",         "VN", "Asia/Ho_Chi_Minh"),
]
for bid, name, city, country, tz in branches:
    cur.execute("""
        INSERT INTO branches (id, name, city, country, currency, total_rooms, timezone, is_active)
        VALUES (%s,%s,%s,%s,'VND',%s,%s,true)
        ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name
    """, (bid, name, city, country, 80, tz))

# ── 2. KPI Targets ────────────────────────────────────────────────────────────
print("Inserting KPI targets...")
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='kpi_targets'")
kpi_cols = [r[0] for r in cur.fetchall()]
print("  kpi_targets cols:", kpi_cols)

for bid, *_ in branches:
    for month in range(1, 13):
        cur.execute("""
            INSERT INTO kpi_targets
              (branch_id, year, month, target_revenue_native, target_revenue_vnd, predicted_occ_pct)
            VALUES (%s, 2026, %s, %s, %s, %s)
            ON CONFLICT (branch_id, year, month) DO UPDATE
              SET target_revenue_native=EXCLUDED.target_revenue_native
        """, (bid, month,
              random.randint(2_000_000_000, 4_000_000_000),
              random.randint(2_000_000_000, 4_000_000_000),
              round(random.uniform(0.65, 0.85), 4)))

# ── 3. Reservations (last 18 months) ─────────────────────────────────────────
print("Inserting reservations...")
COUNTRIES = [
    ("VN", "Vietnam",        40),
    ("US", "United States",   8),
    ("GB", "United Kingdom",  5),
    ("AU", "Australia",       6),
    ("KR", "South Korea",     7),
    ("JP", "Japan",           6),
    ("SG", "Singapore",       5),
    ("FR", "France",          4),
    ("DE", "Germany",         3),
    ("CN", "China",           6),
    ("TH", "Thailand",        3),
    ("IN", "India",           3),
    ("CA", "Canada",          2),
    ("IT", "Italy",           2),
]
OTA_CHANNELS  = ["booking.com", "expedia", "agoda", "airbnb", "direct", "direct", "direct"]
SOURCES_CAT   = {"booking.com":"ota","expedia":"ota","agoda":"ota","airbnb":"ota","direct":"direct"}
STATUS_VALS   = ["checked_out","checked_out","checked_out","checked_out","confirmed","cancelled"]

today = date.today()
start_date = today - timedelta(days=548)

import uuid as _uuid
reservation_idx = 1
for bid, *_ in branches:
    d = start_date
    while d <= today:
        daily_count = random.randint(2, 12)
        for _ in range(daily_count):
            nights = random.randint(1, 7)
            checkout = d + timedelta(days=nights)
            cc, cname, _ = random.choices(COUNTRIES, weights=[w for _,_,w in COUNTRIES])[0]
            ch = random.choice(OTA_CHANNELS)
            adr = random.randint(600_000, 2_000_000)
            total = adr * nights
            status = random.choice(STATUS_VALS)
            res_uuid = str(_uuid.uuid4())
            cur.execute("""
                INSERT INTO reservations
                  (id, branch_id, cloudbeds_reservation_id,
                   guest_country, guest_country_code,
                   source, source_category,
                   check_in_date, check_out_date, nights,
                   grand_total_native, grand_total_vnd, status, reservation_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
            """, (
                res_uuid, bid, f"CB-{reservation_idx:08d}",
                cname, cc,
                ch, SOURCES_CAT.get(ch,"direct"),
                d, checkout, nights,
                total, total, status, d
            ))
            reservation_idx += 1
        d += timedelta(days=1)
    print(f"  {bid}: {reservation_idx} reservations")

# ── 4. Daily metrics (last 90 days) ──────────────────────────────────────────
print("Inserting daily_metrics...")
for bid, *_ in branches:
    for i in range(90):
        day = today - timedelta(days=i)
        rooms = 80
        occ = round(random.uniform(0.45, 0.95), 4)
        rooms_sold = int(rooms * occ)
        adr = random.randint(700_000, 1_600_000)
        revpar = int(occ * adr)
        revenue = rooms_sold * adr
        cancels = random.randint(0, 3)
        new_bk = rooms_sold + cancels
        cur.execute("""
            INSERT INTO daily_metrics
              (branch_id, date, rooms_sold, total_sold, occ_pct, room_occ_pct,
               revenue_native, revenue_vnd, adr_native, revpar_native,
               new_bookings, cancellations, cancellation_pct)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (branch_id, date) DO UPDATE
              SET revenue_native=EXCLUDED.revenue_native
        """, (
            bid, day,
            rooms_sold, rooms_sold,
            occ, occ,
            revenue, revenue,
            adr, revpar,
            new_bk, cancels,
            round(cancels / new_bk if new_bk else 0, 4)
        ))

# ── 5. Events ─────────────────────────────────────────────────────────────────
print("Inserting events...")
sample_events = [
    ("Tet Holiday",           today - timedelta(days=60), today - timedelta(days=53), True),
    ("International Tourism Expo", today + timedelta(days=10), today + timedelta(days=12), True),
    ("National Day",          today + timedelta(days=45), today + timedelta(days=46), False),
    ("MICE Conference 2026",  today + timedelta(days=20), today + timedelta(days=22), True),
    ("Long Weekend",          today + timedelta(days=30), today + timedelta(days=32), False),
]
branch_hn = "11111111-1111-1111-1111-111111111101"
for i, (name, s, e, key) in enumerate(sample_events, 1):
    cur.execute("""
        INSERT INTO events (id, branch_id, city, event_name, event_date_from, event_date_to, is_key_event)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO NOTHING
    """, (str(_uuid.uuid4()), branch_hn, "Hanoi", name, s, e, key))

conn.commit()
cur.close()
conn.close()
print("Seed complete!")
