# newsdigest

Two-week experiment: can an automated pipeline cluster India news feeds by
story and summarise each cluster well enough to be worth reading?

No backend, no database server. A SQLite file, a GitHub Actions workflow, and
a static page on GitHub Pages. If the clustering and the summaries aren't
good, that's a two-weekend loss instead of a two-month one.

Right now, before clustering/summarising exist, the feed-check stage already
produces something worth using on its own: a swipeable card deck
(`reports/index.html`, published via Pages) of every article the live feeds
are currently carrying — title, source, time, a snippet, and a lead image
where the feed has one. Drag or use the arrow keys to move between articles;
tap "Read full article" to open one. That's a byproduct of validating the
feeds, not the end goal — see Pass marks below for what actually decides if
this experiment succeeds.

## Pass marks (fixed before the build, so they can't be rationalised later)

| | Bar |
|---|---|
| Clustering | ≥85% of clusters clean — no two stories merged, no one story split |
| Summaries | 75% self-sufficient — you'd act on it without opening the source |
| The real one | On day 12, do you open the digest without forcing yourself? |

## Stages

```
ingest -> extract -> embed -> cluster -> summarise -> digest
```

Each stage is independently re-runnable. Raw text is fetched once and never
re-fetched; re-clustering at a new threshold touches only the cluster tables.
That keeps the tuning loop at seconds rather than minutes.

## Status

- [x] Feed health check
- [x] Schema
- [x] Article deck (swipeable viewer, published via Pages) — validation tool, not the deliverable
- [ ] Ingest + extraction
- [ ] Golden set (hand-labelled day one)
- [ ] Embed + cluster
- [ ] Summarise
- [ ] Daily digest

## Running the feed check

Actions tab → **Check feeds** → **Run workflow**. Takes about a minute.
Writes `reports/feed_check.md`, `reports/feed_check.json`, and
`reports/index.html`, and pushes them back to the repo.

The workflow also deploys `reports/` to GitHub Pages. **One-time setup:**
Settings → Pages → Source → **GitHub Actions**. After that, the run's job
summary links straight to the deck.

Feeds marked DEAD or STALE get deleted from `feeds.yaml` or their URL fixed.

## Judgments

Every cluster in a digest carries an id like `C-0142`. Verdicts get appended
to `judgments.md` one line at a time:

```
C-0142 split
C-0155 good
C-0161 merged  Kerala floods + Assam floods same cluster
```

This is the only manual step in the whole two weeks, and it's the one that
makes it an experiment rather than a hobby project.

## Audit notes (things already fixed — don't reintroduce them)

- `entry_time` uses `calendar.timegm`, not `time.mktime`. feedparser's
  `*_parsed` is UTC; `mktime` reads it as local time, a silent 5.5h shift in
  IST that would corrupt the clustering window.
- `strip_html` unescapes **before** stripping tags, then again after. RSS
  bodies are frequently escaped markup (`&lt;p&gt;`); the other order leaves
  visible tags in the text.
- Feeds are retried with a browser User-Agent on 403/406/429 (after a 2s
  pause). Several Indian publishers block anything that doesn't look like a
  browser.
- Request `TIMEOUT` is 12s, not 20s. At 20s the worst case was 14.3 min of
  network wait against a 15 min workflow cap -- the job could be killed
  mid-run. Cap is now 25 min and worst case is 9.4 min. If you raise TIMEOUT,
  redo that arithmetic.
- Schema does **not** set `journal_mode = WAL`. WAL is persistent, and
  `news.db-wal` is gitignored — committed rows would silently vanish on push.
- `render_html`'s article deck renders from a JSON payload client-side (needed
  to drive the swipe/filter/queue state), so titles and snippets go through
  `innerHTML` in the browser. Every interpolated text field goes through the
  page's `esc()` first, and article/image URLs go through `safeUrl()` (only
  `http`/`https` pass) — otherwise a hostile feed's `<title>` or `<link>`
  becomes script execution or a `javascript:` URI in someone's browser.

## QA performed

End-to-end, not just unit-level:

- 18 hostile feed fixtures: gzip, Atom, ISO-8859-1, UTF-8 Malayalam with BOM,
  `content:encoded` vs stub `description`, double-escaped CDATA, malformed XML,
  future-dated entries, undated entries, a 4 MB feed, empty channel, an HTML
  page served as a feed, 301 redirect, bot-blocking 403, permanent 403,
  HTTP 500, and a 30s-hang server. 18/18 verdicts correct.
- Clean-venv install from `requirements.txt`.
- The workflow's commit step executed verbatim against a real bare remote:
  first push, idempotent no-op re-run, and a rejected push recovering via
  rebase without clobbering the other commit.
- `feeds.yaml` validation: bad indentation, empty list, missing key, feed
  missing url, duplicate names, missing file. All give a readable message
  instead of a traceback.
- Schema re-runs idempotently; UNIQUE and FK constraints verified to fire.
- Article deck manually tested against synthetic articles with a malicious
  `<script>` source name, an `onerror`-bearing title, and `javascript:` link
  and image URLs — all rendered as inert text, nothing executed.

**Not covered:** the 21 real publisher feeds. They cannot be reached from the
environment this was built in, so their URLs, block behaviour, and body shapes
are unverified until the workflow runs for real — including whether their
images actually resolve, and whether real headline/snippet text ever breaks
the deck layout in a way synthetic fixtures didn't catch.
