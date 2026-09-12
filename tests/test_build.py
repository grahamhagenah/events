"""Tests for build.py, against saved samples of each source (tests/fixtures), so they run offline and a change
that breaks a reader fails here instead of on the live page. Run with: python3 -m unittest discover -s tests

The samples are real responses, trimmed to a few items, except ticketmaster.json: the Discovery API needs a
key, so that one follows its documented format. When a site changes and its reader is updated, save a fresh
sample alongside the fix.
"""

import os
import sys
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
# Which sample stands in for which address.
ROUTES = [
    ("aegwebprod", "roadrunner.json"),
    ("sinclaircambridge.com", "sinclair.xml"),
    ("mideastclub.com", "mideast.html"),
    ("brattlefilm.org", "brattle.html"),
    ("houseofblues.com", "houseofblues.html"),
    ("coolidge.org/showtimes", "coolidge.html"),
    ("drafthouse.com", "alamo.json"),
    ("boxofficeapi/schedule", "landmark_schedule.json"),
    ("boxofficeapi/movies", "landmark_movies.json"),
    ("app.ticketmaster.com", "ticketmaster.json"),
    ("cambridgema.gov", "cambridge.ics"),
]


def sample(url, **kwargs):
    for pattern, name in ROUTES:
        if pattern in url:
            return (FIXTURES / name).read_text()
    raise AssertionError(f"no sample for {url}")


def source(kind, url, category="music", name="Somewhere"):
    return {"kind": kind, "url": url, "category": category, "name": name}


class Readers(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(build, "fetch", sample)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, kind, url, category="music", name="Somewhere"):
        found, error = build.READERS[kind](source(kind, url, category, name)), None
        self.assertTrue(found, f"{kind} read nothing from its sample")
        for item in found:
            self.assertTrue(item["title"].strip())
            self.assertIsInstance(item["date"], date)
            self.assertTrue(all(isinstance(moment, time) for moment in item["times"]))
            self.assertTrue(item["link"].startswith("http"), item["link"])
            self.assertEqual(item["category"], category)
        return found

    def test_aeg(self):
        found = self.read("aeg", "https://aegwebprod.blob.core.windows.net/json/events/219/events.json")
        self.assertEqual(len(found), 3)
        self.assertTrue(all(item["times"] for item in found))
        self.assertTrue(any(item["detail"].startswith("with ") for item in found))

    def test_rss_dates_from_titles(self):
        found = self.read("rss", "https://www.sinclaircambridge.com/events/rss")
        self.assertFalse(any(" on " in item["title"][-15:] for item in found), "the date should come off the title")

    def test_ticketweb_venue_without_room(self):
        found = self.read("ticketweb", "https://mideastclub.com/")
        self.assertEqual(len(found), 3)
        self.assertTrue(all(item["times"] for item in found))
        self.assertFalse(any(" - " in item["venue"] or " – " in item["venue"] for item in found))

    def test_jsonld_brattle_titles_without_showtimes(self):
        found = self.read("jsonld", "https://brattlefilm.org/coming-soon/", "film")
        self.assertFalse(any("@" in item["title"] for item in found))

    def test_jsonld_house_of_blues(self):
        self.read("jsonld", "https://boston.houseofblues.com/shows")

    def test_coolidge_each_day(self):
        found = self.read("coolidge", "https://coolidge.org/showtimes", "film")
        self.assertTrue(all(item["times"] and "coolidge.org/films/" in item["link"] for item in found))

    def test_alamo_skips_past_showings(self):
        found = self.read("alamo", "https://drafthouse.com/s/mother/v2/schedule/market/boston", "film")
        sessions = __import__("json").loads(sample("drafthouse.com"))["data"]["sessions"]
        self.assertEqual(len(found), sum(session["status"] != "PAST" for session in sessions))
        self.assertTrue(all("drafthouse.com/boston/show/" in item["link"] for item in found))

    def test_landmark_titles(self):
        found = self.read("landmark", "X019B", "film")
        titles = {movie["title"] for movie in __import__("json").loads(sample("boxofficeapi/movies"))}
        self.assertTrue({item["title"] for item in found} <= titles)

    def test_ticketmaster_keeps_concerts_only(self):
        with mock.patch.dict(os.environ, {"TICKETMASTER_KEY": "test"}):
            found = self.read("ticketmaster", "KovZ917AJnQ")
        names = {item["title"]: item for item in found}
        self.assertIn("Oteil Burbridge with LaMP - Wish Benefit Tour", names)
        self.assertNotIn("Troy Hawke", names, "Arts & Theatre shows are left out")
        self.assertNotIn("Cancelled Show", names)
        self.assertEqual(names["Late Show, Time To Be Announced"]["times"], [])
        self.assertIn("Untyped Show", names, "a show without a type keeps the venue's category")

    def test_ticketmaster_without_key_is_skipped(self):
        with mock.patch.dict(os.environ, {"TICKETMASTER_KEY": ""}):
            self.assertEqual(build.read_ticketmaster(source("ticketmaster", "X")), [])

    def test_ics(self):
        self.read("ics", "https://www.cambridgema.gov/arts/Calendar.ics", "film")


class Films(unittest.TestCase):
    def test_film_key_matches_spellings(self):
        year = datetime.now(build.BOSTON).year
        self.assertEqual(build.film_key("Coyote vs. ACME"), build.film_key("Coyote vs. Acme"))
        self.assertEqual(build.film_key("The Odyssey (Digital)"), build.film_key("The Odyssey"))
        self.assertEqual(build.film_key("Akira (4K Restoration)"), build.film_key("Akira"))
        self.assertEqual(build.film_key(f"Tony ({year})"), build.film_key("Tony"))
        self.assertNotEqual(build.film_key("Hope (1987)"), build.film_key(f"Hope ({year})"))

    def test_combine_films(self):
        day = date(2026, 9, 20)
        showing = lambda title, venue, hour: {  # noqa: E731
            "title": title, "date": day, "times": [time(hour)], "link": "https://x", "detail": "",
            "venue": venue, "category": "film", "source": venue,
        }
        rows = build.combine_films([
            showing("Coyote vs. ACME", "Alamo Drafthouse", 15),
            showing("Coyote vs. Acme", "Coolidge Corner", 14),
            showing("Paris, Texas", "Brattle", 19),
        ])
        combined = [row for row in rows if "showings" in row]
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0]["title"], "Coyote vs. Acme", "the plainer spelling")
        self.assertEqual(combined[0]["times"], [time(14)], "from its earliest showing")
        self.assertEqual(len(rows), 2)


class Fallback(unittest.TestCase):
    built = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    venue = source("aeg", "https://example.com", name="Roadrunner")

    def listing(self, days_ahead, title="Show"):
        return build.event(self.venue, title, date(2026, 9, 20) + timedelta(days=days_ahead), time(20))

    def previous(self, hours_old, days_ahead=2):
        fetched = (self.built - timedelta(hours=hours_old)).isoformat()
        return {"sources": {"Roadrunner": {"fetched": fetched, "events": [build.saved(self.listing(days_ahead, "Kept"))]}}}

    def test_fresh(self):
        events, failed, stale, listings, errors = build.gather([(self.venue, [self.listing(1)], None)], {}, self.built)
        self.assertEqual(([e["title"] for e in events], failed, stale, errors), (["Show"], [], [], {}))
        self.assertEqual(listings["Roadrunner"]["fetched"], self.built.isoformat())

    def test_error_uses_recent_listings(self):
        events, failed, stale, listings, errors = build.gather([(self.venue, [], OSError("520"))], self.previous(6), self.built)
        self.assertEqual([e["title"] for e in events], ["Kept"])
        self.assertEqual([name for name, _ in stale], ["Roadrunner"])
        self.assertEqual(listings["Roadrunner"]["fetched"], (self.built - timedelta(hours=6)).isoformat(),
                         "the copy keeps its age, so it still expires")
        self.assertIn("Roadrunner", errors)

    def test_error_with_old_listings_fails(self):
        events, failed, stale, *_ = build.gather([(self.venue, [], OSError("520"))], self.previous(72), self.built)
        self.assertEqual((events, failed, stale), ([], ["Roadrunner"], []))

    def test_empty_after_having_shows_uses_listings(self):
        events, failed, stale, _, errors = build.gather([(self.venue, [], None)], self.previous(3), self.built)
        self.assertEqual([e["title"] for e in events], ["Kept"])
        self.assertEqual(errors["Roadrunner"], "returned no listings")

    def test_empty_and_nothing_before_is_just_empty(self):
        events, failed, stale, _, errors = build.gather([(self.venue, [], None)], {}, self.built)
        self.assertEqual((events, failed, stale, errors), ([], [], [], {}))

    def test_past_listings_are_not_restored(self):
        events, *_ = build.gather([(self.venue, [], OSError("520"))], self.previous(6, days_ahead=-1), self.built)
        self.assertEqual(events, [])

    def test_failing_since_carries_over(self):
        earlier = {"failing": {"Roadrunner": {"since": "2026-09-20T01:00:00+00:00", "error": "520"}}}
        failing = build.still_failing({"Roadrunner": "timed out", "Paradise": "403"}, earlier, self.built)
        self.assertEqual(failing["Roadrunner"], {"since": "2026-09-20T01:00:00+00:00", "error": "timed out"})
        self.assertEqual(failing["Paradise"]["since"], self.built.isoformat())


class Page(unittest.TestCase):
    def test_renders_rows_and_filters(self):
        with mock.patch.object(build, "fetch", sample):
            found = build.read_aeg(source("aeg", "https://aegwebprod.blob.core.windows.net/json/events/219/events.json"))
        page = build.render_index(found, [source("aeg", "x", name="Roadrunner")], [], [], datetime.now(timezone.utc))
        self.assertEqual(page.count('<li data-category="music">'), len(found))
        for key in build.CATEGORIES:
            self.assertIn(f'data-show="{key}"', page)
        self.assertIn("[hidden] {{ display: none !important; }}".replace("{{", "{").replace("}}", "}"), page)


class Alerts(unittest.TestCase):
    def test_opens_for_outages_and_closes_on_recovery(self):
        import json
        import tempfile

        import alerts

        now = datetime.now(timezone.utc)
        failing = {
            "Coolidge Corner": {"since": (now - timedelta(hours=7)).isoformat(), "error": "HTTP Error 520"},
            "Paradise": {"since": (now - timedelta(hours=1)).isoformat(), "error": "timed out"},
        }
        calls = []

        def gh(*args):
            calls.append(args)
            if args[:2] == ("issue", "list"):
                return json.dumps([{"number": 7, "title": "Source failing: Brattle"}])
            return ""

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as record:
            json.dump({"failing": failing}, record)
        with mock.patch.object(alerts, "gh", gh), mock.patch.object(sys, "argv", ["alerts.py", record.name]):
            alerts.main()
        os.unlink(record.name)

        created = [args for args in calls if args[:2] == ("issue", "create")]
        closed = [args for args in calls if args[:2] == ("issue", "close")]
        self.assertEqual([args[3] for args in created], ["Source failing: Coolidge Corner"], "only the 7-hour outage")
        self.assertEqual([args[2] for args in closed], ["7"], "Brattle loads again")


if __name__ == "__main__":
    unittest.main()
