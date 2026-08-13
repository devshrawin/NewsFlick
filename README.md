# NewsFlick

Live: <https://devshrawin.github.io/NewsFlick/> — repo:
<https://github.com/devshrawin/NewsFlick>

(codename `newsdigest` internally — localStorage keys and the internal
`_newsdigest_*` identifiers are unchanged from before the display-name
rebrand, to avoid breaking existing users' saved data. The **repo itself**
was renamed from `newsdigest` to `NewsFlick`; the old
`devshrawin.github.io/newsdigest/` Pages URL now 404s, `git remote` and
`UA_BOT` are already updated to the new URL — update both again if this
ever moves.)

**What this actually is, honestly:** started as a two-week experiment in
clustering India news feeds by story and auto-summarising them. The
clustering/summarising stages below were never built. What exists instead —
the feed-check + swipeable card deck, originally meant as a throwaway
validation tool — absorbed essentially all the effort and is the real
product: a personal, editorial-styled news reader, self-updating every
~45 minutes, no backend beyond a GitHub Actions job and a static page. The
"Pass marks" and "Stages" sections below are kept as a record of the
original goal and an honest admission that it isn't what got built — not
a claim that it's in progress.

No backend, no database server, no build step. A GitHub Actions workflow and
a static page on GitHub Pages. `pipeline/db.py`/`schema.sql` exist as a
forward-looking design for the clustering stages *if* that work ever
resumes — they are not wired into anything today. Real persistence exists,
but through `data/articles.jsonl` (git-diffable JSONL, not the SQLite file
the schema implies), not through those files — see "Persistence" below.

The deck (`docs/index.html`, published via Pages) shows every article the
live feeds are currently carrying that has a lead image (see "No-image
filter" below) — title, source, a snippet, and the image rendered below the
headline/summary rather than as a top masthead band, no caption on it. No
fixed cap on how many cards show, and no visible story-count anywhere in the
UI (see "Deck size" below for both). Drag or use the arrow keys to move
between articles; tap "Read full article" (top-right of each card) to open
one. Dark by default (light only if the browser explicitly asks for it, or
the user picks Light/Dark/Auto from the drawer). A hamburger button opens a
collapsible left drawer holding three independently-foldable sections, in
order: **Interests** (multi-select topic filter, pick as many as you like or
none to see everything), **World** (single-select region/continent filter),
and **Sources** (single-select; a source with zero results under the
current Interests/World filters greys out instead of leading to an empty
view). First-time visitors get a one-time "what do you want to see" form for
Interests; skip it or revisit it anytime from the drawer — both write to the
same `localStorage`-backed selection. Cards carry a political-leaning pill
where a source has one (`source_bias.yaml`, hand-curated, cited where it
comes from a public rating like AllSides — most sources, especially Indian
ones, are honestly "Not rated" rather than guessed at); no background tint,
by design (see the leaning audit note below). Near-duplicate headlines from
different agencies merge into one card (see "Dedupe performance" below —
this used to be the pipeline's dominant cost), and each card has a
save-for-later star and a share button (deep-links back to that exact card
on our own page, not the source, so the digest itself stays visible when
shared; the exported share image mirrors whatever theme — light or dark —
is active). A small badge next to the header wordmark shows how many
articles are new since your last visit (see "Persistence" below); tapping
it jumps to the first one. The workflow runs every ~45 minutes on its own,
or on demand from the Actions tab.

## Pass marks (the original goal, not what shipped — kept as a record)

| | Bar |
|---|---|
| Clustering | ≥85% of clusters clean — no two stories merged, no one story split |
| Summaries | 75% self-sufficient — you'd act on it without opening the source |
| The real one | On day 12, do you open the digest without forcing yourself? |

Neither Clustering nor Summaries exist. The deck gets opened daily, which is
the only one of these three actually answered.

## Stages (the original plan — only the first is built)

```
ingest -> extract -> embed -> cluster -> summarise -> digest
```

Each stage was meant to be independently re-runnable, with raw text fetched
once and never re-fetched, so re-clustering at a new threshold would touch
only the cluster tables. `schema.sql` is designed around this. None of it is
wired up — every run re-fetches all feeds from scratch and keeps nothing.

## Status

- [x] Feed health check
- [ ] Schema — designed (`pipeline/schema.sql`), not wired into anything;
  real persistence went a different route (see "Persistence" below)
- [x] Article deck (swipeable viewer, published via Pages) — this is the actual product now
- [x] Persistence — `data/articles.jsonl`, first-seen tracking + cached
  full-text extraction (see "Persistence" below)
- [x] Full-text extraction — best-effort, budgeted, backlog drains over
  multiple runs (see "Persistence" below)
- [ ] Golden set (hand-labelled day one)
- [ ] Embed + cluster
- [ ] Summarise
- [ ] Daily digest

## Persistence

`data/articles.jsonl` — one JSON object per line, sorted by id (git-diffable
and delta-compresses across the ~32 commits/day the scheduler makes; a
rewritten-every-run SQLite file couldn't). Started as `first_seen.jsonl`
(just an id → timestamp map, renamed+extended once it grew a second job —
see the audit note below for the exact row shape and why one file, not two).
Two things it does:

- **First-seen tracking.** Every article's id gets a timestamp the first run
  that sees it, carried forward unchanged after that. `render_html()` exposes
  it as each card's `firstSeen`; the client compares it against a
  `localStorage` `newsdigest:lastVisit` timestamp to show the "N new" badge
  and jump to the first new article on click. Entries for ids absent from
  the current run get pruned once older than `ARTICLES_RETENTION_DAYS` (14).
- **Full-text extraction**, via `trafilatura`. `extract_full_text()` fetches
  the article's own URL and pulls the real body text past the ~240-char RSS
  teaser, exposed as each card's `fullText`. Capped at
  `EXTRACT_BUDGET_PER_RUN` (40) attempts per run — a network fetch per
  article isn't free, so the backlog drains gradually across runs instead of
  adding 40×(fetch time) to every single loop iteration. A successful
  extraction is cached forever (`extract_ok: true`, never retried); a failed
  one (`extract_note` says why — HTTP status, paywall/interstitial, timeout)
  is retried on a later run.

`seen` (which articles you've swiped past *this session*) is still
`sessionStorage`-only and unrelated to any of this — closing the tab forgets
it, by design; it answers "what have I scrolled past right now", not "what's
new since I was last here".

## Known gaps (largest first)

- **No embed/cluster/summarise.** The persistence layer above is what those
  stages would need, but they're not built — see "Stages" above. Full-text
  is now stored; nothing downstream reads it yet except the card itself.
- Report stats: `total_items`/`live` count only feeds graded verdict `OK`,
  but articles are drawn from every feed that returned parseable entries
  (`STALE`/`FUTURE`/`NO DATES` included) — the report labels these
  separately (`contributing` feeds vs. `live` feeds) so the two counts don't
  read as the same population.

## Running the feed check

One job loops internally -- check feeds, commit, `sleep` 45 minutes,
repeat -- for up to ~5h40m, then a coarse `schedule` trigger (every 6h)
starts the next block. Or run it on demand from Actions tab →
**Check feeds** → **Run workflow**. Each iteration writes
`docs/feed_check.md`, `docs/feed_check.json`, and
`docs/index.html`, and pushes them back to the repo.

Two things tried and rejected before this, for the record:

- **GitHub's own `schedule` trigger, alone.** Checked the actual run
  history (`.../actions/workflows/{id}/runs` via the REST API): even a
  single *hourly* cron went 4+ hours with zero scheduled fires on this
  repo. `schedule` is best-effort with no SLA and no catch-up for a
  dropped tick -- true at any frequency here, not just sub-hourly ones.
- **An external cron service (cron-job.org) calling `workflow_dispatch`.**
  Worked, but needed a third-party account plus a personal access token
  to babysit (expiry, rotation) for something GitHub can do to itself.

The `sleep`-loop keeps the actual 45-min cadence inside one running job,
which doesn't depend on GitHub's scheduler at all once started. The
`schedule` trigger only has to fire roughly every 6 hours to restart the
chain -- coarse, infrequent triggers are far less prone to the
congestion/drop behavior above, and even a several-hour delay there barely
matters.

**One-time setup, both required:**

1. Settings → Pages → Source → **Deploy from a branch** → `main` /
   `/docs` (classic branch-based Pages only offers `/(root)` or `/docs`
   as the folder choice, hence the pipeline writing there and not
   `/reports`). Branch-based Pages auto-republishes on every push to
   `docs/` with no deploy step needed -- required because
   `actions/deploy-pages` can only run once per job, not repeatedly from
   inside a loop.
2. Nothing else -- `permissions: contents: write` (already in the
   workflow) is all a loop iteration needs to check, commit, and push.

Feeds marked DEAD or STALE get deleted from `feeds.yaml` or their URL fixed.

## Deck size

68 feeds yield roughly 3,300 articles a run, ~3,150 after merging. No fixed
cap on the deck (`DECK_LIMIT` existed, got removed — see the audit note
below); every deduped, image-having article shows. Cards with no lead image
*are* excluded (see "No-image filter" below) — currently around a third of
the deduped set, leaving roughly 2,000+ cards in the actual deck. That's a
real jump in payload size from the old 400-card cap: `index.html` grows to
roughly 1.5+ MB raw / ~450 KB+ gzipped (Pages serves gzipped) from the old
~300 KB / ~90 KB — still loads fine, no pagination needed yet, but if the
feed count keeps growing this is the number to watch. `feed_check.json`
still has the full un-deduped set regardless of what made the deck.

## No-image filter

Cards with no lead image are dropped, not shown text-only — this has
flipped twice (see the audit note below for the full history): dropped
originally because a missing image looked broken in the old top-masthead
layout, un-dropped once the image moved to a band below the text (a missing
one didn't look broken there), then re-dropped again on explicit
preference. If you're the one reading this deciding whether to flip it a
third time: check with whoever owns this reader first, it's clearly a
real, contested preference and not just leftover code.

## Dedupe performance

`dedupe_articles` compares each article's title against every existing
cluster. The straightforward version of this is O(n²) and was, measured,
the pipeline's dominant cost at real scale (~14 min projected at ~3,000
articles) — the reason the "45-min" loop was actually landing every ~53 min.
Fixed with a token index (only compare against clusters sharing a
significant word) plus `SequenceMatcher`'s free `quick_ratio()`/
`real_quick_ratio()` upper bounds before the real comparison — both exact
filters, not heuristics, verified byte-identical to the original algorithm
by `tests/test_pipeline.py::test_dedupe_matches_naive_reference` across 40
seed/size combinations. ~6x measured speedup at realistic vocabulary
(87.8s → 13.8s at 1,000 articles) — better, not fully linear; worth another
pass if the feed count grows a lot further. The original algorithm is kept
verbatim as `_dedupe_articles_naive` purely as that test's reference —
never called from the pipeline.

## Tests

`tests/test_pipeline.py` (`pip install -r requirements-dev.txt`, then
`pytest tests/`) — pure-function unit tests, no network, one test per bullet
in "Audit notes" below, plus the dedupe equivalence fuzz test above. This is
the only automated testing in the project; everything else in "QA
performed" was manual and one-time.

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
  network wait, which mattered against the job's old single-run time cap.
  The relevant cap since the sleep-loop scheduler shipped is different: a
  single check_feeds.py run now needs to fit well inside the 45-min loop
  interval, not the job's overall 350-min budget, or the sleep stacks on
  top and cadence drifts. If you raise TIMEOUT or add feeds, redo that
  arithmetic against 45 min, not the job timeout.
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
- The workflow's use of GitHub's `schedule` trigger has changed twice.
  The original fix was offsetting off `:00`, the most congested cron slot
  on GitHub; that helped some but run-history data (checked via the REST
  API weeks later) showed `schedule` still drifting by hours regardless
  of offset. Next it was dropped for an external cron pinger calling
  `workflow_dispatch` — worked, but needed a third-party account and a
  PAT to babysit. Current design (see "Running the feed check" above)
  drops the external dependency too: a coarse `schedule` (every 6h,
  infrequent enough to actually fire reliably) just restarts a job that
  loops the real 45-min cadence internally via `sleep`.
- `render()`'s full DOM rebuild on every `advance()` is why a swipe used to
  visibly jump: the promoted card popped straight to its final position
  instead of animating there, since a freshly-created element has nothing
  to transition from. `promoteAfterFlyOut()` mutates the *existing* stacked
  cards' `--i`/transform in place instead, so the CSS transition actually
  runs. Don't route forward navigation back through a full `render()`
  without checking whether this still applies.
- `.onb-scrim` (the onboarding backdrop) sits at z-index 15, below the
  header's 20, on purpose — it used to be 50 (above everything), so a
  first-visit tap on the hamburger landed on the backdrop instead of the
  button and silently just dismissed the prompt. The backdrop still gates
  the deck itself (`.layout` is z-index 1); only the header stays reachable
  through it.
- `source_bias.yaml` leaning labels feed a pill on cards (no background tint
  — removed in the editorial redesign, kept off on purpose). An unlisted
  source (or the file missing entirely) must resolve to "Not rated" with no
  pill at all — never silently to "Center" — since that's a claim about a
  real news organization's politics that most sources here (deliberately)
  don't have backing for.
- The no-image deck filter was removed once the card's image moved from a
  top masthead band to a band below the text. It existed only because a
  missing image looked broken in the old layout; `cardMarkup()` already
  renders cleanly with none (`media == ''`). Don't reintroduce it without
  re-checking that reasoning against whatever the card layout looks like
  by then.
- `fetch()` flags a response `_newsdigest_truncated` when `MAX_BYTES` cuts a
  feed off mid-stream, and `check()` surfaces it in the row's note. Without
  this a truncated feed just silently parsed with fewer entries (or a bozo
  warning) and nothing said why.
- `--hue`/`source_hue()` were removed project-wide (payload field, `chip()`
  parameter, per-card/per-queue-item CSS custom property) once the
  editorial redesign replaced every hue-based accent (avatar circles, queue
  bars) with ink/mono styling — the variable was being set but consumed by
  zero CSS rules. The share-image canvas was the one remaining real
  consumer (a hue-gradient avatar); it now pulls the live theme's actual
  `--ink`/`--bg`/`--sub` custom properties instead, so the exported PNG
  matches whichever theme (light/dark) is active rather than a fixed
  identity left over from the pre-redesign dark-glass look.
- `<meta name="theme-color">` is two media-scoped tags (dark/light), not one
  hardcoded value — a light-theme PWA install used to get a dark status bar
  regardless. `applyTheme()` overwrites both tags' `content` directly when
  the user picks an explicit Light/Dark (a `<meta>` tag can't itself be
  scoped to a `[data-theme]` attribute selector the way CSS can) and resets
  them to their own OS-scoped default on Auto.
- The card's "lead image · from feed" caption and the "Story N of M · drag
  either way" status line above the stage are both removed on explicit
  preference — not a bug fix, a deliberate simplification. Don't add either
  back without checking first.
- `DECK_LIMIT` and `round_robin_by_source()` are gone entirely, not just
  disabled — the round-robin function existed solely to pick a fair subset
  when the cap forced *something* to be dropped; with no cap it did nothing
  but re-sort by time, so it was deleted along with its tests rather than
  left as dead code (see "Deck size" above for the real number now, and the
  payload-size tradeoff of removing the cap).
- The no-image deck filter has been flipped three times now (dropped → kept
  text-only → dropped again) as the card layout and preferences both
  changed — see "No-image filter" above. The current state (dropped) is
  deliberate, current, and explicitly re-confirmed; it is not an oversight
  waiting to be "fixed" back.
- `data/articles.jsonl` was `data/first_seen.jsonl` — renamed (via `git mv`,
  history preserved) once it grew a second job. Row shape extended from
  `{"id", "first_seen"}` to `{"id", "first_seen", "raw_text", "extract_ok",
  "extract_note"}`; `load_articles_store()` reads old-shape rows fine since
  every field access goes through `.get()` with a default — no migration
  script needed, the old shape is just a valid subset of the new one.
- The repo itself was renamed from `newsdigest` to `NewsFlick` on GitHub.
  `git remote`, `UA_BOT`'s self-identifying URL, and this README's live
  links were all updated in the same pass — grep for
  `devshrawin/newsdigest` or `devshrawin.github.io/newsdigest` before
  trusting any old link found elsewhere (chat history, notes, bookmarks).

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
- Swipe/hamburger/layout fixes verified against a real 32-feed run
  (28 OK, 1954 cards): promoted card reuses its existing DOM node on
  advance (not a fresh element); filter change still does a full,
  correct rebuild; next-section suggestion tested by exhausting a
  2-article topic filter and confirming "Continue to X" lands correctly
  filtered; desktop measured 664×365 (landscape) vs mobile 310×421
  (portrait) on the same content; hamburger bug reproduced with a real
  dispatched click (not a synthetic `.click()`, which bypasses hit-testing
  and would have missed this) and confirmed fixed via `elementFromPoint` +
  a real click, both with the onboarding prompt open and after dismissing
  it. Political-leaning pill/tint confirmed present for a rated source
  (BBC World: Center, "via AllSides"), absent for an unrated one (Times of
  India), on both synthetic and real generated data.

**Update 2026-08-03:** all 32 feeds now in `feeds.yaml` were reachable and
validated from this dev environment (27 OK, 5 STALE-but-alive, 0 dead) — a
big batch of ~60 candidate URLs pulled from elsewhere came back 29 dead
(moved, 403/404, or in Reuters/AP's case, publicly discontinued RSS entirely),
so don't trust a pasted feed list without re-running this check. Still
unconfirmed: whether the GitHub Actions runner's network/IP gets blocked
differently than this environment did, whether lead images actually resolve
at scale, and whether real headline/snippet text ever breaks the deck layout
in a way synthetic fixtures didn't catch.
