"""
Setup real branches with actual Cloudbeds property IDs.
Run: python setup_branches.py
"""
import os
import psycopg2

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

# Real branch definitions
BRANCHES = [
    {
        "id":   "11111111-1111-1111-1111-111111111101",
        "name": "HiD Taipei",
        "city": "Taipei",
        "country": "TW",
        "currency": "TWD",
        "total_rooms": 30,
        "timezone": "Asia/Taipei",
        "cloudbeds_property_id": "25496",
        "cloudbeds_api_key": _key("25496"),
    },
    {
        "id":   "11111111-1111-1111-1111-111111111102",
        "name": "HiD Saigon",
        "city": "Ho Chi Minh City",
        "country": "VN",
        "currency": "VND",
        "total_rooms": 40,
        "timezone": "Asia/Ho_Chi_Minh",
        "cloudbeds_property_id": "185944",
        "cloudbeds_api_key": _key("185944"),
    },
    {
        "id":   "11111111-1111-1111-1111-111111111103",
        "name": "HiD 1948",
        "city": "Hanoi",
        "country": "VN",
        "currency": "VND",
        "total_rooms": 35,
        "timezone": "Asia/Ho_Chi_Minh",
        "cloudbeds_property_id": "22872",
        "cloudbeds_api_key": _key("22872"),
    },
    {
        "id":   "11111111-1111-1111-1111-111111111104",
        "name": "HiD Oani",
        "city": "Da Nang",
        "country": "VN",
        "currency": "VND",
        "total_rooms": 25,
        "timezone": "Asia/Ho_Chi_Minh",
        "cloudbeds_property_id": "318301",
        "cloudbeds_api_key": _key("318301"),
    },
    {
        "id":   "11111111-1111-1111-1111-111111111105",
        "name": "HiD Osaka",
        "city": "Osaka",
        "country": "JP",
        "currency": "JPY",
        "total_rooms": 30,
        "timezone": "Asia/Tokyo",
        "cloudbeds_property_id": "301582",
        "cloudbeds_api_key": _key("301582"),
    },
]

conn = psycopg2.connect(**DB)
cur = conn.cursor()

# Check if cloudbeds_api_key column exists
cur.execute("""
    SELECT column_name FROM information_schema.columns
    WHERE table_name='branches'
""")
cols = [r[0] for r in cur.fetchall()]
print("Existing columns:", cols)

# Add cloudbeds_api_key column if missing
if "cloudbeds_api_key" not in cols:
    cur.execute("ALTER TABLE branches ADD COLUMN cloudbeds_api_key TEXT")
    print("  Added cloudbeds_api_key column")

for b in BRANCHES:
    cur.execute("""
        INSERT INTO branches
          (id, name, city, country, currency, total_rooms, timezone,
           cloudbeds_property_id, cloudbeds_api_key, is_active)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,true)
        ON CONFLICT (id) DO UPDATE SET
          name=EXCLUDED.name,
          city=EXCLUDED.city,
          country=EXCLUDED.country,
          currency=EXCLUDED.currency,
          total_rooms=EXCLUDED.total_rooms,
          timezone=EXCLUDED.timezone,
          cloudbeds_property_id=EXCLUDED.cloudbeds_property_id,
          cloudbeds_api_key=EXCLUDED.cloudbeds_api_key,
          is_active=true
    """, (
        b["id"], b["name"], b["city"], b["country"],
        b["currency"], b["total_rooms"], b["timezone"],
        b["cloudbeds_property_id"], b["cloudbeds_api_key"]
    ))
    print(f"  Upserted: {b['name']} (property {b['cloudbeds_property_id']})")

conn.commit()
cur.close()
conn.close()
print("\nBranches setup complete!")
print("\nBranch IDs for reference:")
for b in BRANCHES:
    print(f"  {b['name']:<20} → {b['id']}")
