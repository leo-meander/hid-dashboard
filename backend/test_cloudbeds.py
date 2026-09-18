"""
Test Cloudbeds API connection for all properties.
Run: python test_cloudbeds.py
"""
import os
import urllib.request
import json

PROPERTIES = [
    ("HiD Taipei",  "25496"),
    ("HiD Saigon",  "185944"),
    ("HiD 1948",    "22872"),
    ("HiD Oani",    "318301"),
    ("HiD Osaka",   "301582"),
]

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

for name, prop_id in PROPERTIES:
    api_key = _key(prop_id)
    url = f"{BASE}/getProperty?propertyID={prop_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("success"):
                p = data.get("data", {})
                print(f"OK  {name}: {p.get('propertyName','?')} | rooms={p.get('roomCount','?')}")
            else:
                print(f"FAIL {name}: {data.get('message','unknown error')}")
    except Exception as e:
        print(f"ERR  {name}: {e}")
