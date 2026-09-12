#!/usr/bin/env python3
"""Look up Ticketmaster venue ids by name, for ticketmaster lines in sources.txt.

Run it from GitHub (Actions → Find a Ticketmaster venue), where the TICKETMASTER_KEY secret is, or locally:
TICKETMASTER_KEY=… python3 find_venue.py "Brighton Music Hall" "Crystal Ballroom"
"""

import json
import os
import sys
from urllib.parse import urlencode

from build import fetch

names = sys.argv[1:] or [name.strip() for name in os.environ.get("NAMES", "").split(",") if name.strip()]
for name in names:
    query = urlencode({"apikey": os.environ["TICKETMASTER_KEY"], "keyword": name, "stateCode": "MA", "size": 10})
    found = json.loads(fetch(f"https://app.ticketmaster.com/discovery/v2/venues.json?{query}"))
    venues = (found.get("_embedded") or {}).get("venues", [])
    print(f"{name}:")
    for venue in venues:
        city = (venue.get("city") or {}).get("name", "")
        upcoming = (venue.get("upcomingEvents") or {}).get("_total", 0)
        print(f"  {venue['id']}  {venue['name']}, {city}  ({upcoming} upcoming)")
    if not venues:
        print("  no matches in Massachusetts")
