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
tap "Read full article" to open one. Dark by default (light only if the
browser explicitly asks for it). A hamburger button opens a collapsible left
drawer holding two independently-foldable sections: **Interests**, a
multi-select topic filter (pick as many as you like, or none to see
everything), and **Sources**, single-select as before. First-time visitors
get a one-time "what do you want to see" form for Interests; skip it or
revisit it anytime from the drawer — both write to the same
`localStorage`-backed selection. Near-duplicate headlines from different
agencies merge into one card, and each card has a save-for-later star and a
share button (deep-links back to that exact card on our own page, not the
source, so the digest itself stays visible when shared). The workflow runs
hourly on its own, or on demand from the Actions tab. That's all a byproduct of
validating the feeds, not the end goal — see Pass marks below for what
actually decides if this experiment succeeds.

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

Runs automatically every hour (`schedule` trigger), or on demand from
Actions tab → **Check feeds** → **Run workflow**. Takes about a minute.
Writes `reports/feed_check.md`, `reports/feed_check.json`, and
`reports/index.html`, and pushes them back to the repo.

GitHub disables `schedule` triggers on a repo after 60 days with no activity
at all (any push resets the clock) — re-enable it from the Actions tab if a
long-idle repo stops running.

The workflow also deploys `reports/` to GitHub Pages. **One-time setup:**
Settings → Pages → Source → **GitHub Actions**. After that, the run's job
summary links straight to the deck.

Feeds marked DEAD or STALE get deleted from `feeds.yaml` or their URL fixed.

## Deck size

32 feeds yield roughly 2,000 articles an hour, ~1,900 after merging. Embedded
as JSON that made `index.html` 1.3 MB — a slow load on the phone this is meant
to be read on, for a deck nobody swipes a tenth of. `DECK_LIMIT` (400) keeps
the newest N; the run prints how many were dropped rather than pretending it
showed everything, and `feed_check.json` still has the full set.

## Topics

Each article gets a topic tag (Politics, Business, Sports, Entertainment,
Technology, World, Health, or General) from a plain keyword-hit count over
the title and snippet — see `TOPIC_KEYWORDS` in `pipeline/check_feeds.py`.
It's a stopgap, not classification: a story with none of the listed words
falls back to General, and a story that genuinely spans two topics (a
government bailout of an airline, say) gets whichever topic's keywords hit
more, not both. Add/adjust keywords there directly; there's no config file
for it yet.

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
- `dedupe_articles`'s cross-agency merge only compares articles within
  `DEDUPE_WINDOW_HOURS` of each other. Without that guard, a recurring
  generic headline ("Sensex closes higher") from the same publisher on two
  different days would merge across days and silently eat a real story.
- A cluster's `anchor`/`at` stay pinned to the article that **opened** it,
  even when a better-looking representative takes over. Moving them with the
  rep made membership depend on arrival order: a headline similar to the
  original but not the new rep started a duplicate card, and one similar only
  to the new rep got pulled in transitively — dropping a distinct story.
- `also_from` is derived at the end from the cluster's member list minus the
  rep's own source. Appending as-you-go left a source in its own "also on"
  line once it had been demoted and then later won the rep slot back.
- Article `id` is `sha1(link or source+title)[:10]` — stable across runs as
  long as the publisher URL doesn't change, and that's what both the share
  deep-link (`#<id>`) and the `localStorage` saved list key off. The
  `source+title` fallback matters: `entry_link()` returns `""` for entries
  with no `<link>`, and every one of those hashed to the same id, so starring
  one starred them all.
- Topic keywords are matched with `\b` word boundaries, not substrings.
  `"who "` as a substring filed *"The MLA who quit the party"* under Health,
  and a bare `"ai"` would hit "said".
- `entry_image` skips `<img>` tags with a declared width/height under 100 and
  known pixel hosts/paths. A 1×1 tracker **loads successfully**, so the
  `onerror` fallback never fires — `object-fit: cover` just stretched it into
  a solid colour block across the top of the card.
- Every `write_text`/`read_text` passes `encoding="utf-8"` explicitly. Without
  it Python uses the platform default — cp1252 on Windows — and one `₹` in a
  real headline aborts the run. The Actions runner defaults to UTF-8, so this
  only ever broke local runs, making the script look Linux-only.
- The deck's fly-out animation has a `setTimeout` watchdog alongside
  `anim.finished`. A hidden or throttled tab never composites, so the
  animation never finishes and the promise never settles — without the
  watchdog the `busy` flag stayed set and the deck wedged permanently.
- The fly-out captures `renderSeq` and only advances if it still matches.
  Clicking a filter mid-swipe otherwise let the pending callback increment
  past the index that render had just chosen, skipping an article.
- Saved ids are pruned against the current snapshot on load. They accumulate
  across hourly rebuilds but only ids still present can be displayed, so the
  chip counted articles the Saved view could not show.

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
- Full pipeline run against all 32 live feeds: 2,014 articles → 1,928 after
  merging 86 duplicates, 0 articles with an empty link, all ids unique, and 0
  cases of a representative appearing in its own `also_from`.
- 12 real publisher image URLs fetched in-browser: all 12 loaded, none a
  tracking pixel (smallest 620×450), confirming the pixel filter on real data.
- Deck driven through the browser: next/prev, progress rail, topic∧source
  filtering, a contradictory filter pair (empty-state card), save + the Saved
  pseudo-filter, `localStorage` persistence, queue-jump, `S` to save, the
  end-of-queue card and Start over, and a deep link into an article hidden by
  the active filter (clears filters and lands on it). Bogus `#hash` ignored
  without blanking the deck. No console errors; no horizontal overflow at
  375 px or 1280 px; dark mode resolves (`color-mix()` computes).
- Drawer redesign driven through the browser on a real generated page:
  onboarding form's topic picks reflect live in the drawer's Interests section
  and vice versa (shared state, not two copies), interests persist across
  reload without the form reappearing, drawer open/close/backdrop/Escape,
  independent Interests/Sources section collapse, and confirmed dark is the
  default palette when the OS reports dark and the light override applies
  when it explicitly reports light. Card-to-controls gap measured at 28px
  (mobile) / 15px (desktop) — was visually overlapping before.

**Update 2026-08-03:** all 32 feeds now in `feeds.yaml` were reachable and
validated from this dev environment (27 OK, 5 STALE-but-alive, 0 dead) — a
big batch of ~60 candidate URLs pulled from elsewhere came back 29 dead
(moved, 403/404, or in Reuters/AP's case, publicly discontinued RSS entirely),
so don't trust a pasted feed list without re-running this check. Still
unconfirmed: whether the GitHub Actions runner's network/IP gets blocked
differently than this environment did, whether lead images actually resolve
at scale, and whether real headline/snippet text ever breaks the deck layout
in a way synthetic fixtures didn't catch.
