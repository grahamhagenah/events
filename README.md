# Events

The events page, https://events.grahamhagenah.com/, is built in [grahamhagenah/news](https://github.com/grahamhagenah/news), alongside the newsfeed: its code is in that repo's `events/` folder, and its venues are listed in [`events/sources.txt`](https://github.com/grahamhagenah/news/blob/main/events/sources.txt).

This repo only serves the finished page. The `gh-pages` branch holds it, and news's build replaces that branch whenever the page is rebuilt, using a deploy key (Settings → Deploy keys). This branch's history has the code as it was before it moved.
