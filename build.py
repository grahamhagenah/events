#!/usr/bin/env python3
"""Fetch every source in sources.txt and write the coming weeks' Boston-area events to dist/."""

import html
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.txt"
OUT_DIR = ROOT / "dist"
REPO_URL = "https://github.com/grahamhagenah/events"
DAYS_AHEAD = 30  # How far ahead the page lists events.
BOSTON = ZoneInfo("America/New_York")
USER_AGENT = "Mozilla/5.0 (compatible; events-feed/1.0)"
CATEGORIES = {"music": "Music", "film": "Film"}


def read_sources():
    """sources.txt lines: how to read the source, its URL, a category, and a name."""
    sources = []
    for line in SOURCES_FILE.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            kind, url, category, *name = line.split()
            sources.append({"kind": kind, "url": url, "category": category, "name": " ".join(name)})
    return sources


def fetch(url, attempts=2, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except Exception as error:
            # Timeouts, dropped connections and 5xx errors are often momentary; a 404 won't change.
            momentary = not isinstance(error, urllib.error.HTTPError) or error.code >= 500
            if not momentary or attempt == attempts - 1:
                raise
            time.sleep(2)


def text(markup):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup or ""))).strip()


def event(source, title, day, start=None, link="", detail="", venue=""):
    """One listing. start is a time of day in Boston, or None when the source gives only the date."""
    return {
        "title": title,
        "date": day,
        "times": [start] if start else [],
        "link": link or source["url"],
        "detail": detail,
        "venue": venue or source["name"],
        "category": source["category"],
        "source": source["name"],
    }


def at_boston(moment):
    """An aware datetime's date and time of day in Boston; a naive one is taken to be Boston time already."""
    local = moment.astimezone(BOSTON) if moment.tzinfo else moment
    return local.date(), local.time()


# Readers: each takes a source and returns its events.


def read_aeg(source):
    """AEG Presents venue sites (Roadrunner) load every listing from one events.json."""
    events = []
    for item in json.loads(fetch(source["url"]))["events"]:
        if not item.get("active") or item.get("private"):
            continue
        day, start = at_boston(datetime.fromisoformat(item["eventDateTimeISO"]))
        titles = item["title"]
        support = re.sub(r"\s*,\s*", ", ", titles.get("supportingText") or "").strip(" ,")
        events.append(event(
            source,
            titles.get("eventTitleText") or titles.get("headlinersText"),
            day,
            start,
            link=(item.get("ticketing") or {}).get("url", ""),
            detail=f"with {support}" if support else "",
        ))
    return events


def read_rss(source):
    """AXS venue feeds (The Sinclair) give the show date only at the end of the title: "… on Sep 12, 2026"."""
    events = []
    for item in re.findall(r"<item>(.*?)</item>", fetch(source["url"]), re.S):
        title = text(re.search(r"<title>(.*?)</title>", item, re.S).group(1))
        link = text((re.search(r"<link>(.*?)</link>", item, re.S) or re.search(r"()", "")).group(1))
        match = re.fullmatch(r"(.*) on ([A-Z][a-z]{2} \d{1,2}, \d{4})", title)
        if match:
            events.append(event(source, match.group(1), datetime.strptime(match.group(2), "%b %d, %Y").date(), link=link))
    return events


def read_ticketweb(source):
    """Venue sites using TicketWeb's WordPress listing (The Middle East). Dates there leave out the year."""
    today = datetime.now(BOSTON).date()
    events = []
    for section in fetch(source["url"]).split('class="tw-section"')[1:]:
        name = re.search(r'class="tw-name">\s*<a[^>]*href="([^"]+)"[^>]*title="Event Name - (.*?) \| (\d{1,2} [A-Za-z]+) (\d{1,2}:\d{2} [AP]M)"', section)
        if not name:
            continue
        link, title, day_month, clock = name.groups()
        day = datetime.strptime(f"{day_month} {today.year}", "%d %B %Y").date()
        if day < today - timedelta(days=60):  # A January show listed in December.
            day = day.replace(year=today.year + 1)
        room = re.search(r'class="tw-venue-name">(.*?)</span>', section, re.S)
        events.append(event(
            source,
            html.unescape(title),
            day,
            datetime.strptime(clock, "%I:%M %p").time(),
            link=html.unescape(link),
            # "@ Middle East - Zuzu": just the venue, not the room; Sonia, next door, stays Sonia.
            venue=text(room.group(1)).lstrip("@ ").split(" - ")[0] if room else "",
        ))
    return events


def read_jsonld(source):
    """Pages that describe their screenings or shows as schema.org Events (the Brattle's Coming Soon page)."""
    found = []

    def collect(data):
        if isinstance(data, list):
            for item in data:
                collect(item)
        elif isinstance(data, dict):
            kinds = data.get("@type") if isinstance(data.get("@type"), list) else [data.get("@type")]
            if any(isinstance(kind, str) and kind.endswith("Event") for kind in kinds) and data.get("startDate"):
                found.append(data)
            for value in data.values():
                if isinstance(value, (dict, list)):
                    collect(value)

    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', fetch(source["url"]), re.S):
        try:
            collect(json.loads(block))
        except ValueError:
            continue

    events = []
    for item in found:
        day, start = at_boston(datetime.fromisoformat(item["startDate"]))
        # The Brattle adds the showtime to each name: "Filipiñana - 9/12/26 @ 12:00 pm".
        title = re.sub(r"\s+-\s+\d{1,2}/\d{1,2}/\d{2,4}\s+@.*$", "", html.unescape(item.get("name", "")))
        events.append(event(source, title, day, start if "T" in item["startDate"] else None, link=item.get("url", "")))
    return events


def read_coolidge(source):
    """The Coolidge's showtimes page shows one day at a time (?date=2026-09-14) for about a week ahead."""
    first = fetch(source["url"])
    days = sorted(set(re.findall(r'data-date="(\d{4}-\d{2}-\d{2})"', first)))
    events = []
    for day_text in days:
        page = first if day_text == days[0] else fetch(f"{source['url']}?date={day_text}")
        day = date.fromisoformat(day_text)
        for card in page.split('<div class="film-card">')[1:]:
            film = re.search(r'class="film-card__link" title="([^"]+)" href="([^"]+)"', card)
            if not film:
                continue
            listing = event(source, html.unescape(film.group(1)), day, link=f"https://coolidge.org{film.group(2)}")
            listing["times"] = [
                datetime.strptime(clock.strip().upper(), "%I:%M%p").time()
                for clock in re.findall(r'class="showtime-ticket__time">([^<]+)<', card)
            ]
            events.append(listing)
    return events


def read_alamo(source):
    """Alamo Drafthouse loads a market's whole schedule (Boston: the Seaport) from one JSON file."""
    data = json.loads(fetch(source["url"]))["data"]
    titles = {item["slug"]: (item.get("show") or {}).get("title") for item in data["presentations"]}
    market = urlsplit(source["url"]).path.rstrip("/").rsplit("/", 1)[-1]
    events = []
    for session in data["sessions"]:
        title = titles.get(session["presentationSlug"])
        if session.get("isHidden") or session.get("status") in ("PAST", "CANCELED", "CANCELLED") or not title:
            continue
        # The theater's local time; a midnight show is dated the night it starts, not the business day.
        start = datetime.fromisoformat(session["showTimeClt"])
        link = f"https://drafthouse.com/{market}/show/{session['presentationSlug']}"
        events.append(event(source, title, start.date(), start.time(), link=link))
    return events


LANDMARK_API = "https://www.landmarktheatres.com/api/gatsby-source-boxofficeapi"


def read_landmark(source):
    """Landmark theaters (Kendall Square), by theater id, through the schedule service their site uses."""
    today = datetime.now(BOSTON).date()
    theater = json.dumps({"id": source["url"], "timeZone": "America/New_York"}, separators=(",", ":"))
    query = urlencode({
        "from": f"{today}T03:00:00",
        "to": f"{today + timedelta(days=DAYS_AHEAD + 1)}T03:00:00",
        "theaters": theater,
    })
    schedule = json.loads(fetch(f"{LANDMARK_API}/schedule?{query}"))[source["url"]]["schedule"]
    if not schedule:
        return []
    # The schedule has only film ids; titles come separately, all at once.
    films = json.loads(fetch(f"{LANDMARK_API}/movies?" + urlencode([("ids", film) for film in schedule])))
    titles = {film["id"]: film["title"] for film in films}
    events = []
    for film, days in schedule.items():
        for showings in days.values():
            for showing in showings:
                if showing.get("isExpired") or film not in titles:
                    continue
                start = datetime.fromisoformat(showing["startsAt"])
                tickets = next(
                    (entry["urls"][0] for entry in (showing.get("data") or {}).get("ticketing", [])
                     if entry.get("type") == "DESKTOP" and entry.get("urls")),
                    "",
                )
                events.append(event(source, titles[film], start.date(), start.time(), link=tickets))
    return events


# Ticketmaster's own type for each show ("segment"). Concerts and films are listed; other types, like the
# comedy, drag and theater nights (Arts & Theatre) some music venues host, are left out. A show without a
# type keeps the venue's category.
TICKETMASTER_SEGMENTS = {"Music": "music", "Film": "film"}


def read_ticketmaster(source):
    """Ticketmaster venues (the Paradise), by Discovery API venue id. The API needs a free key, read from the
    TICKETMASTER_KEY environment variable (a repository secret on GitHub); without one these are skipped."""
    key = os.environ.get("TICKETMASTER_KEY")
    if not key:
        print(f"  {source['name']}: skipped, no TICKETMASTER_KEY", file=sys.stderr)
        return []
    query = urlencode({"apikey": key, "venueId": source["url"], "size": 100, "sort": "date,asc"})
    data = json.loads(fetch(f"https://app.ticketmaster.com/discovery/v2/events.json?{query}"))
    events = []
    for item in (data.get("_embedded") or {}).get("events", []):
        dates = item.get("dates") or {}
        start = dates.get("start") or {}
        if not start.get("localDate") or (dates.get("status") or {}).get("code") == "cancelled":
            continue
        # Ticketmaster gives the show's own local date and time, which for these venues is Boston's.
        clock = None
        if start.get("localTime") and not start.get("timeTBA"):
            clock = datetime.strptime(start["localTime"], "%H:%M:%S").time()
        types = item.get("classifications") or []
        primary = next((kind for kind in types if kind.get("primary")), types[0] if types else {})
        segment = (primary.get("segment") or {}).get("name")
        if segment and segment not in TICKETMASTER_SEGMENTS:
            continue
        listing = event(source, item["name"], date.fromisoformat(start["localDate"]), clock, link=item.get("url", ""))
        listing["category"] = TICKETMASTER_SEGMENTS.get(segment, source["category"])
        events.append(listing)
    return events


def read_ics(source):
    """iCalendar feeds. Only each event's first date; repeating events aren't expanded."""
    def unescape(value):
        return re.sub(r"\\([,;\\])", r"\1", value).replace("\\n", " ").strip()

    lines = re.sub(r"\r?\n[ \t]", "", fetch(source["url"])).splitlines()  # Undo line folding.
    events, fields = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            fields = {}
        elif line == "END:VEVENT" and fields is not None:
            # Each field is (parameters, value), as in DTSTART;TZID=America/New_York:20260919T120000.
            params, value = fields.get("DTSTART", ("", ""))
            if re.fullmatch(r"\d{8}", value):
                day, start = datetime.strptime(value, "%Y%m%d").date(), None
            else:
                moment = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
                zone = re.search(r"TZID=([^;]+)", params)
                if value.endswith("Z"):
                    moment = moment.replace(tzinfo=timezone.utc)
                elif zone:
                    moment = moment.replace(tzinfo=ZoneInfo(zone.group(1)))
                day, start = at_boston(moment)
            venue = unescape(fields.get("LOCATION", ("", ""))[1]).split(",")[0]
            summary = unescape(fields.get("SUMMARY", ("", ""))[1])
            events.append(event(source, summary, day, start, link=fields.get("URL", ("", ""))[1], venue=venue))
            fields = None
        elif fields is not None and ":" in line:
            name, value = line.split(":", 1)
            key, _, params = name.partition(";")
            fields.setdefault(key, (params, value))
    return events


READERS = {
    "aeg": read_aeg,
    "rss": read_rss,
    "ticketweb": read_ticketweb,
    "jsonld": read_jsonld,
    "coolidge": read_coolidge,
    "ticketmaster": read_ticketmaster,
    "alamo": read_alamo,
    "landmark": read_landmark,
    "ics": read_ics,
}


def load(source):
    try:
        return source, READERS[source["kind"]](source), None
    except Exception as error:
        return source, [], error


def merge_showings(events):
    """One row per film (or show) per day and place, with all its times, instead of a row per showing."""
    merged = {}
    for item in events:
        key = (item["date"], item["venue"], item["title"].casefold())
        if key in merged:
            merged[key]["times"] = sorted(set(merged[key]["times"] + item["times"]))
        else:
            merged[key] = dict(item, times=sorted(item["times"]))
    return list(merged.values())


FORMAT_NOTE = re.compile(r"\s*\([^)]*\b(digital|4k|35mm|70mm|16mm|imax|3d|dcp|restoration|restored)\b[^)]*\)", re.I)


def film_key(title):
    """A film's name as theaters' different spellings of it share: "Coyote vs. ACME" and "Coyote vs. Acme",
    "The Odyssey (Digital)" and "The Odyssey", "Tony (2026)" and "Tony". Only this and next year's dates go,
    which theaters add to new releases, so an older film of the same name stays apart."""
    year = datetime.now(BOSTON).year
    title = FORMAT_NOTE.sub("", title)
    title = re.sub(rf"\s*\(({year}|{year + 1})\)", "", title)
    return re.sub(r"\W+", " ", title).casefold().strip()


def combine_films(items):
    """A day's film playing at two or more places becomes one row, with each place's times inside it."""
    films = {}
    rows = []
    for item in items:
        if item["category"] == "film":
            films.setdefault(film_key(item["title"]), []).append(item)
        else:
            rows.append(item)
    for showings in films.values():
        if len({item["venue"] for item in showings}) < 2:
            rows += showings
            continue
        showings.sort(key=lambda item: item["times"][:1])
        rows.append({
            "showings": showings,
            # The plainest of the names: without a format note ("The Odyssey", not "The Odyssey (Digital)"),
            # then with the fewest capitals ("Coyote vs. Acme", not "Coyote vs. ACME"), the same every day.
            "title": min((item["title"] for item in showings), key=lambda title: (len(title), sum(map(str.isupper, title)))),
            "times": sorted(moment for item in showings for moment in item["times"])[:1],
            "category": "film",
        })
    return rows


# Rendering.


def clock(moment):
    """8pm, 7:30pm."""
    hour = moment.hour % 12 or 12
    return f"{hour}{'' if moment.minute == 0 else f':{moment.minute:02d}'}{'am' if moment.hour < 12 else 'pm'}"


# Each row's mark for its category, in the category's color: two beamed eighth notes for music, a frame of
# film with sprocket holes down both sides for film. Drawn once in the page; rows point to the drawing.
ICON_DRAWINGS = {
    "music": '<path d="M5.5 12.5V4l8-2v8.5"/><circle cx="3.75" cy="12.5" r="1.75" fill="currentColor"/>'
             '<circle cx="11.75" cy="10.5" r="1.75" fill="currentColor"/>',
    "film": '<rect x="2" y="2" width="12" height="12" rx="1.5"/>'
            '<path d="M5 2v12M11 2v12M2 5.5h3M2 10.5h3M11 5.5h3M11 10.5h3"/>',
}
ICON_SYMBOLS = '<svg class="symbols" aria-hidden="true">' + "".join(
    f'<symbol id="icon-{name}" viewBox="0 0 16 16"><g fill="none" stroke="currentColor" stroke-width="1.5" '
    f'stroke-linecap="round" stroke-linejoin="round">{drawing}</g></symbol>'
    for name, drawing in ICON_DRAWINGS.items()
) + "</svg>"


def icon(category, decorative=False):
    """A category's mark. Beside a label that already says it (the filter), it's hidden from screen readers."""
    label = "aria-hidden=\"true\"" if decorative else f'role="img" aria-label="{html.escape(CATEGORIES[category])}"'
    return f'<svg class="icon" {label}><use href="#icon-{category}"/></svg>'


def render_times(moments):
    # data-time lets the page drop today's showings once they've started.
    times = "".join(f'<time data-time="{moment:%H:%M}">{clock(moment)}</time>' for moment in moments)
    return f'<span class="times">{times}</span>' if times else ""


def render_row(item):
    """Laid out like the newsfeed: the venue on the left, then the name with that day's times after it."""
    if "showings" in item:
        return render_combined(item)
    detail = f'<span class="detail">{html.escape(item["detail"])}</span>' if item["detail"] else ""
    return (
        f'<li data-category="{item["category"]}"><span class="source">{icon(item["category"])}'
        f'<span>{html.escape(item["venue"])}</span></span>'
        f'<div class="headline"><a class="title" href="{html.escape(item["link"])}">{html.escape(item["title"])}</a>'
        f'{render_times(item["times"])}{detail}</div></li>'
    )


def render_combined(item):
    """A film at several places: one row reading "4 theaters … from 12:10pm" that opens to each place's times."""
    places = len({showing["venue"] for showing in item["showings"]})
    first = item["times"][0] if item["times"] else None
    start = f'<span class="times">from <time class="from" data-time="{first:%H:%M}">{clock(first)}</time></span>' if first else ""
    showings = "".join(
        f'<li><a href="{html.escape(showing["link"])}">{html.escape(showing["venue"])}</a>{render_times(showing["times"])}</li>'
        for showing in item["showings"]
    )
    return (
        f'<li class="combined" data-category="{item["category"]}"><details><summary>'
        f'<span class="source">{icon(item["category"])}<span>{places} theaters</span></span>'
        f'<div class="headline"><span class="title">{html.escape(item["title"])}</span>{start}'
        f'<span class="more" aria-hidden="true">›</span></div></summary>'
        f'<ul class="showings">{showings}</ul></details></li>'
    )


def render_index(events, sources, failed, built_at):
    days = {}
    for item in events:
        days.setdefault(item["date"], []).append(item)

    sections = []
    for day in sorted(days):
        # Untimed listings first, then by time, then by name.
        rows = combine_films(days[day])
        rows.sort(key=lambda item: (bool(item["times"]), item["times"][:1], item["title"].casefold()))
        sections.append(
            f'<section class="day" data-date="{day.isoformat()}">'
            f'<h2><span class="relative"></span><span class="date">{day.strftime("%a, %b")} {day.day}</span></h2>\n'
            '<ul>\n' + "\n".join(render_row(item) for item in rows) + "\n</ul></section>"
        )

    buttons = '<button data-show="all">All</button>' + "".join(
        f'<button data-show="{key}">{icon(key, decorative=True)}{label}</button>' for key, label in CATEGORIES.items()
    )
    names = ", ".join(html.escape(source["name"]) for source in sources)
    failed_note = f"<p>Couldn’t load {html.escape(', '.join(failed))}.</p>\n" if failed else ""
    body = (
        f'<nav class="filter" aria-label="Show">{buttons}</nav>\n'
        + "\n".join(sections)
        + '\n<p class="empty" hidden>Nothing coming up.</p>\n<nav class="pager"></nav>\n'
        f"<footer>\n{failed_note}<p>From {names}.</p>\n"
        f'<p><a href="{REPO_URL}">Add a source</a></p>\n</footer>\n'
        f"<script>{INDEX_JS}</script>"
    )
    return page("Events", body, built_at)


INDEX_JS = """
  // Today and Tomorrow, from this device's clock, so an older build still reads right; days already
  // past are hidden until the next build drops them.
  const key = d => d.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const today = key(new Date());
  const tomorrow = key(new Date(Date.now() + 86400000));
  const days = [...document.querySelectorAll(".day")];
  for (const day of days) {
    if (day.dataset.date < today) day.remove();
    else day.querySelector(".relative").textContent =
      day.dataset.date === today ? "Today" : day.dataset.date === tomorrow ? "Tomorrow" : "";
  }

  // Today lists only what's still to come: showings drop off once they've started, and an event goes
  // once its last one has. Events without a time stay all day.
  const now = new Date().toLocaleTimeString("en-GB", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit" });
  const todays = document.querySelector(`.day[data-date="${today}"]`);
  if (todays) {
    for (const li of todays.querySelectorAll(":scope > ul > li")) {
      const times = li.querySelectorAll("time:not(.from)");
      for (const t of times) if (t.dataset.time < now) t.remove();
      // In a film at several places, a place with nothing left today goes too.
      for (const place of li.querySelectorAll(".showings li")) if (!place.querySelector("time")) place.remove();
      if (times.length && !li.querySelector("time:not(.from)")) { li.remove(); continue; }
      const from = li.querySelector("time.from");
      if (from) {
        const next = [...li.querySelectorAll(".showings time")].sort((a, b) => a.dataset.time < b.dataset.time ? -1 : 1)[0];
        from.dataset.time = next.dataset.time;
        from.textContent = next.textContent;
        const places = li.querySelectorAll(".showings li");
        li.querySelector(".source > span").textContent = places.length > 1 ? places.length + " theaters" : places[0].querySelector("a").textContent;
      }
    }
    // Reorder what's left by its next showing, as the build ordered everything by its first. Listings
    // without a time stay at the top.
    const next = el => (el.querySelector("time.from") || el.querySelector("time"))?.dataset.time || "";
    const byNext = (a, b) => next(a) < next(b) ? -1 : next(a) > next(b) ? 1 : 0;
    for (const list of todays.querySelectorAll(":scope > ul, .showings")) [...list.children].sort(byNext).forEach(el => list.append(el));
    if (!todays.querySelector("li")) todays.remove();
  }

  // The filter shows every category or just one, and is remembered in this browser. Three days show at a
  // time, counting only days with something the filter shows; ?page=2 shows the next three.
  const DAYS_PER_PAGE = 3;
  const filter = document.querySelector(".filter");
  const pager = document.querySelector(".pager");
  let show = "all";
  try { show = localStorage.getItem("events-show") || "all"; } catch (error) {}
  function showEvents() {
    const shownDays = [];
    for (const day of document.querySelectorAll(".day")) {
      let shown = 0;
      for (const li of day.querySelectorAll(":scope > ul > li")) {
        li.hidden = show !== "all" && li.dataset.category !== show;
        shown += !li.hidden;
      }
      day.hidden = true;
      day.classList.remove("first");
      if (shown) shownDays.push(day);
    }
    const pages = Math.max(1, Math.ceil(shownDays.length / DAYS_PER_PAGE));
    const page = Math.min(pages, Math.max(1, parseInt(new URLSearchParams(location.search).get("page")) || 1));
    const onPage = shownDays.slice((page - 1) * DAYS_PER_PAGE, page * DAYS_PER_PAGE);
    for (const day of onPage) day.hidden = false;
    onPage[0]?.classList.add("first");
    const link = (n, text) => `<a href="${n === 1 ? location.pathname : "?page=" + n}">${text}</a>`;
    pager.innerHTML = pages < 2 ? "" :
      (page > 1 ? link(page - 1, "← Earlier") : "<span></span>") +
      `<span>Page ${page} of ${pages}</span>` +
      (page < pages ? link(page + 1, "Later →") : "<span></span>");
    document.querySelector(".empty").hidden = shownDays.length > 0;
    for (const b of filter.children) b.setAttribute("aria-pressed", b.dataset.show === show);
    document.querySelector("main").classList.add("paged");
  }
  showEvents();
  filter.addEventListener("click", event => {
    const b = event.target.closest("button");
    if (!b) return;
    show = b.dataset.show;
    try { localStorage.setItem("events-show", show); } catch (error) {}
    history.replaceState(null, "", location.pathname); // Back to page one.
    showEvents();
  });
"""


def page(title, body, built_at):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-title" content="Events">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<title>{title}</title>
<style>
  /* Category colors, for the marks in the list and on the filter: violet music, amber film. */
  :root {{ --music: #a78bfa; --film: #fbbf24; }}
  html {{ background: #000; }}
  body {{ margin: 0; padding: 3rem 1.25rem 4rem; color: #fff; background: #000;
         font: 17px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }}
  main {{ max-width: 46rem; margin: 0 auto; }}
  header {{ display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; margin-bottom: 2rem; }}
  /* The newsfeed and this page, as a pair: the one you're on in white, the other a gray link to it. */
  .sites {{ display: flex; gap: .9rem; }}
  .sites a, .sites a:visited {{ color: #555; font-size: 1.15rem; font-weight: 700; letter-spacing: -.01em; text-decoration: none; }}
  .sites a:hover {{ color: #999; }}
  .sites a[aria-current], .sites a[aria-current]:hover {{ color: #fff; }}
  .header-note {{ color: #666; font-size: .8rem; white-space: nowrap; }}
  .filter {{ display: flex; flex-wrap: wrap; gap: .4rem 1.1rem; margin: -.75rem 0 1.75rem; }}
  .filter button {{ padding: 0; border: 0; background: none; color: #666; font: inherit; font-size: .8rem; cursor: pointer; }}
  .filter button:hover {{ color: #999; }}
  .filter button[aria-pressed="true"] {{ color: #fff; }}
  /* Each row's music or film mark, and the filter's, in its category's color. */
  .symbols {{ position: absolute; width: 0; height: 0; overflow: hidden; }}
  .icon {{ flex: none; width: 12px; height: 12px; color: var(--dot); }}
  .filter .icon {{ margin-right: .4em; vertical-align: -1px; }}
  [data-show="music"], [data-category="music"] {{ --dot: var(--music); }}
  [data-show="film"], [data-category="film"] {{ --dot: var(--film); }}
  h2 {{ margin: 2.25rem 0 .5rem; color: #777; font-size: .75rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }}
  .day.first h2 {{ margin-top: 0; }}
  /* Until the script picks the page, show the first three days, so the whole month never flashes up. */
  main:not(.paged) .day:nth-of-type(n+4) {{ display: none; }}
  .pager {{ display: flex; justify-content: space-between; margin-top: 2.5rem; color: #666; font-size: .8rem; }}
  .pager a, .pager a:visited {{ color: #999; }}
  .relative:not(:empty) {{ color: #fff; margin-right: .6em; }}
  ul {{ margin: 0; padding: 0; list-style: none; }}
  /* Rows as in the newsfeed: the venue beside its mark, then the name, one line tall, times after it. */
  .day > ul > li, .combined summary {{ display: grid; grid-template-columns: 10rem 1fr; gap: 1.25rem; align-items: baseline; }}
  .day > ul > li {{ position: relative; padding: .4rem 0; }}
  /* Whatever the filter and pager hide stays hidden, however specific the rules that lay it out. */
  [hidden] {{ display: none !important; }}
  /* The mark leads the venue name, inside the text's left edge. */
  .source {{ display: flex; align-items: center; gap: .5em; min-width: 0; color: #666; font-size: .8em; }}
  .source > span {{ min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }}
  .headline {{ display: flex; align-items: baseline; min-width: 0; }}
  .headline .title {{ min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }}
  .times, .detail {{ margin-left: .6em; color: #666; font-size: .8em; white-space: nowrap; }}
  .times {{ flex: none; }}
  .times time + time::before {{ content: ", "; }}
  .detail {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; }}
  /* A film at several places: the row opens to each place's times, with a › that turns when it's open. */
  .day > ul > li.combined {{ display: block; }}
  .combined summary {{ list-style: none; cursor: pointer; }}
  .combined summary::-webkit-details-marker {{ display: none; }}
  .combined summary:hover .title {{ text-decoration: underline; }}
  .more {{ flex: none; display: inline-block; margin-left: .5em; color: #666; font-size: .8em; transition: transform .15s; }}
  .combined details[open] .more {{ transform: rotate(90deg); }}
  .showings {{ margin: .35rem 0 .2rem calc(10rem + 1.25rem); }}
  .showings li {{ display: flex; align-items: baseline; gap: .6em; padding: .15rem 0; font-size: .9em; }}
  .showings a {{ flex: none; color: #ccc; }}
  .showings .times {{ margin-left: 0; white-space: normal; }}
  @media (max-width: 34rem) {{
    /* On a phone one line is too few words, so names wrap in full, below the venue. */
    .day > ul > li, .combined summary {{ grid-template-columns: 1fr; gap: 0; }}
    .showings {{ margin-left: 0; }}
    .headline {{ display: block; }}
    .headline .title, .times, .detail {{ white-space: normal; }}
    /* The mark moves beside the name's first line, below the venue, into a slot at the left edge that the
       venue and name both start after. Its top: the row's padding, then (in the venue's text size) 1.45em
       for the venue's line and .9em for half the name's line, less half the mark. */
    .day > ul > li {{ padding-left: calc(12px + .5em); }}
    .source .icon {{ position: absolute; left: 0; top: calc(.4rem + 2.35em - 6px); }}
  }}
  a {{ color: #fff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .empty {{ color: #666; font-size: .9rem; }}
  footer {{ margin-top: 4rem; color: #666; font-size: .8em; }}
  footer p {{ margin: .4rem 0; }}
  footer a {{ color: #999; text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }}
</style>
</head>
<body>
{ICON_SYMBOLS}
<main>
<header><nav class="sites" aria-label="Sites"><a href="https://news.grahamhagenah.com/">Newsfeed</a><a href="./" aria-current="page">Events</a></nav><span class="header-note">Updated <time class="updated" datetime="{built_at.isoformat()}"></time></span></header>
{body}
</main>
<script>
  for (const t of document.querySelectorAll("time.updated")) {{
    const s = Math.max(60, (Date.now() - new Date(t.dateTime)) / 1000);
    t.textContent = (s < 3600 ? Math.round(s / 60) + "m" : s < 86400 ? Math.round(s / 3600) + "h" : Math.round(s / 86400) + "d") + " ago";
  }}
</script>
</body>
</html>
"""


def main():
    sources = read_sources()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(load, sources))

    today = datetime.now(BOSTON).date()
    last_day = today + timedelta(days=DAYS_AHEAD)
    events, failed = [], []
    for source, found, error in results:
        if error:
            print(f"✗ {source['name']}: {error}", file=sys.stderr)
            failed.append(source["name"])
            continue
        upcoming = [item for item in found if today <= item["date"] <= last_day]
        print(f"✓ {source['name']}: {len(upcoming)} in the next {DAYS_AHEAD} days ({len(found)} listed)")
        events += upcoming

    if not events:
        sys.exit("No events loaded — not writing the page.")

    events = merge_showings(events)
    built_at = datetime.now(timezone.utc)
    OUT_DIR.mkdir(exist_ok=True)
    shutil.copytree(ROOT / "static", OUT_DIR, dirs_exist_ok=True)
    (OUT_DIR / "index.html").write_text(render_index(events, sources, failed, built_at))
    print(f"Wrote {OUT_DIR.relative_to(ROOT)}/index.html with {len(events)} listings")


if __name__ == "__main__":
    main()
