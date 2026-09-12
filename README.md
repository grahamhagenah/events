# Events

A plain list of what's on in Boston, Cambridge and Somerville over the next 30 days: concerts, films and arts events, grouped by day. Live at https://events.grahamhagenah.com/.

- Sources are listed in `sources.txt`, one per line, with how to read each one. Venues rarely publish feeds, so the build uses the most dependable thing each offers: AEG's event data (Roadrunner), an event feed (The Sinclair), schema.org event data (the Brattle, House of Blues), the schedule data behind a theater chain's site (Alamo Drafthouse, Landmark's Kendall Square), Ticketmaster (the Paradise, Brighton Music Hall, the Crystal Ballroom, the Somerville Theatre), iCalendar (Cambridge Arts), or the page itself (the Coolidge, TicketWeb listings at the Middle East). A source that fails is named at the bottom of the page; the rest still show.
- Ticketmaster venues (the Paradise) need a free [Ticketmaster Discovery API](https://developer.ticketmaster.com/) key, kept as the `TICKETMASTER_KEY` repository secret. Without it they're skipped.
- Build locally with `python3 build.py`, then open `dist/index.html`. Uses only the Python standard library.
- GitHub Actions rebuilds and deploys to GitHub Pages every three hours and on every push.
