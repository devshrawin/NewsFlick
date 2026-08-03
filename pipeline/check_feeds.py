"""
Feed health check.

Pings every feed in feeds.yaml and writes:
  reports/feed_check.md    -- human-readable, for reading on a phone
  reports/feed_check.json  -- machine-readable, consumed by the ingest stage

No API keys needed.

Per feed we record:
  ALIVE?   did it return parseable entries
  FRESH?   how old is the newest entry (a feed stuck 3 days back is dead to us)
  STUB?    full article text in the feed, or a two-line teaser
           (nearly all will be teasers -- that's why extraction exists)
"""

import calendar
import difflib
import hashlib
import html
import json
import re
import sys
import time
import statistics
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.yaml"
REPORT_MD = ROOT / "reports" / "feed_check.md"
REPORT_JSON = ROOT / "reports" / "feed_check.json"
REPORT_HTML = ROOT / "reports" / "index.html"

# Polite, identifiable UA tried first.
UA_BOT = "newsdigest/0.1 (personal research; +https://github.com/devshrawin/newsdigest)"
# Several Indian publishers 403 anything that isn't a browser. Retried with this.
UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

TIMEOUT = 12   # 21 feeds x 2 attempts must fit well inside the workflow cap
STUB_THRESHOLD = 400   # chars of body below which we call it a teaser
STALE_HOURS = 48
SKEW_HOURS = 2      # newest entry this far in the FUTURE = broken publisher clock
MAX_BYTES = 8 * 1024 * 1024   # refuse to buffer a runaway feed

# Same wire story (PTI/ANI/Reuters) run near-verbatim by multiple publishers
# tends to have near-identical headlines; unrelated stories rarely score this
# high on normalized-title similarity. High threshold favors under-merging
# (a leftover duplicate card) over over-merging (silently dropping a distinct
# story) -- this is a stopgap title-similarity heuristic, not the semantic
# "Embed + cluster" stage on the roadmap, which is what actually earns the
# clustering pass mark. Tune here, not there.
DEDUPE_TITLE_THRESHOLD = 0.78
DEDUPE_WINDOW_HOURS = 20   # only merge articles whose published times are this close

# 32 feeds produce ~1900 cards an hour, which embedded as JSON made index.html
# 1.3 MB -- a slow load on the phone this is meant to be read on, for a deck
# nobody swipes a tenth of. Newest N survive; the count dropped is printed, not
# swallowed. The full set is still in feed_check.json.
DECK_LIMIT = 400


def strip_html(text: str) -> str:
    """Tags out, entities decoded. Entity decoding matters: without it
    &amp;#8217; style junk inflates length counts and poisons later text."""
    text = html.unescape(text or "")      # &lt;p&gt; -> <p>  (feeds often escape markup)
    text = re.sub(r"<[^>]+>", " ", text)  # now the tags are strippable
    text = html.unescape(text)            # &#8217; -> ’ inside the real text
    return re.sub(r"\s+", " ", text).strip()


def entry_body(entry) -> str:
    """Longest text blob the entry offers."""
    candidates = []
    if entry.get("summary"):
        candidates.append(entry["summary"])
    for c in entry.get("content") or []:
        if c.get("value"):
            candidates.append(c["value"])
    if not candidates:
        return ""
    return strip_html(max(candidates, key=len))


def entry_time(entry):
    """feedparser gives *_parsed as a UTC struct_time.

    calendar.timegm treats it as UTC. time.mktime would treat it as local
    time -- a silent 5.5h shift in IST, which would later corrupt the
    clustering window. Do not swap this back.
    """
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
            except (ValueError, OverflowError):
                pass
    return None


def fetch(url: str):
    """Try polite UA; fall back to a browser UA if the publisher blocks bots."""
    last = None
    agents = (UA_BOT, UA_BROWSER)
    for i, ua in enumerate(agents):
        is_last_attempt = i == len(agents) - 1
        try:
            resp = requests.get(
                url,
                timeout=TIMEOUT,
                headers={"User-Agent": ua, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
                allow_redirects=True,
                stream=True,
            )
            # Buffer at most MAX_BYTES, then drop the connection.
            chunks, total = [], 0
            for chunk in resp.iter_content(65536):
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BYTES:
                    break
            resp._newsdigest_body = b"".join(chunks)
            resp.close()
        except requests.RequestException as exc:
            last = (None, type(exc).__name__)
            continue
        if resp.status_code in (403, 406, 429) and not is_last_attempt:
            last = (resp, f"HTTP {resp.status_code}")
            time.sleep(2)   # FIX 9: don't hammer a server that just refused us
            continue
        note = "browser UA needed" if i > 0 and resp.status_code == 200 else ""
        return resp, note
    resp, err = last
    return resp, err


SNIPPET_LEN = 240   # chars of body shown per article card


def entry_link(entry) -> str:
    if entry.get("link"):
        return entry["link"]
    for l in entry.get("links") or []:
        if l.get("href"):
            return l["href"]
    return ""


IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif)(?:\?|$)", re.IGNORECASE)
IMG_TAG_RE = re.compile(r"<img[^>]*>", re.IGNORECASE)
IMG_SRC_RE = re.compile(r'src=["\']([^"\']+)["\']', re.IGNORECASE)
# A 1x1 tracking pixel loads fine, so the <img onerror> fallback never fires --
# object-fit: cover then stretches it into a solid colour block across the top
# of the card. Feedburner/analytics feeds routinely put one first in the body.
IMG_TINY_RE = re.compile(r'\b(?:width|height)\s*=\s*["\']?\s*([0-9]{1,2})\b', re.IGNORECASE)
IMG_PIXEL_HOST_RE = re.compile(
    r"(?:feedburner|feedsportal|doubleclick|scorecardresearch|/~r/|/~ff/|pixel|/1x1|blank\.gif)",
    re.IGNORECASE,
)

# Keyword-based topic tagging -- a stopgap, not real classification. It's a
# plain word-hit count per topic, so it will misfile anything that doesn't
# use these words (a profile piece on a cricketer with no match count as
# "sports" the way a match report would) or double-count wire copy that
# mixes domains (a "government bails out an airline" story hits both
# Politics and Business). Order matters only as a tie-break -- first topic
# reaching the top hit count wins.
TOPIC_KEYWORDS = {
    "Politics": [
        "parliament", "lok sabha", "rajya sabha", "minister", "modi", "bjp",
        "congress party", "election", "cabinet", "governor", "chief minister",
        "assembly", "opposition", "bill passed", "supreme court", "high court",
    ],
    "Business": [
        "sensex", "nifty", "stock market", "rupee", "economy", "gdp",
        "inflation", "rbi", "ipo", "earnings", "quarterly results", "startup",
        "market cap", "shares", "investors", "trade deal", "tariff",
    ],
    "Sports": [
        "cricket", "match", "tournament", "olympics", "ipl", "football",
        "hockey", "medal", "world cup", "wicket", "innings", "goal scored",
        "tennis", "badminton", "athlete",
    ],
    "Entertainment": [
        "bollywood", "movie", "actor", "actress", "film", "box office",
        "ott release", "celebrity", "music album", "web series", "trailer",
    ],
    "Technology": [
        "artificial intelligence", "ai", "smartphone", "app launch",
        "software", "startup funding", "google", "apple", "meta platforms",
        "chip", "data breach", "cybersecurity",
    ],
    "World": [
        "united states", "china", "pakistan", "russia", "ukraine",
        "united nations", "global summit", "foreign policy", "embassy",
        "president of", "prime minister of",
    ],
    "Health": [
        "covid", "health ministry", "hospital", "vaccine", "disease",
        "doctor", "outbreak", "medical",
    ],
}

# Whole-word matching, built once. Substring matching filed "The MLA who quit
# the party" under Health (the relative pronoun "who" hit the WHO keyword) and
# would equally have matched "ai" inside "said" or "chip" inside "chipped".
# \b handles the punctuation cases (" AI, model") that a space-padded
# substring check missed.
TOPIC_PATTERNS = {
    topic: [re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE) for kw in kws]
    for topic, kws in TOPIC_KEYWORDS.items()
}


def classify_topic(title: str, snippet: str) -> str:
    text = f"{title} {snippet}"
    best_topic, best_hits = "General", 0
    for topic, patterns in TOPIC_PATTERNS.items():
        hits = sum(1 for p in patterns if p.search(text))
        if hits > best_hits:
            best_topic, best_hits = topic, hits
    return best_topic


def entry_image(entry):
    """Best-effort lead image: Media RSS fields first, then an enclosure,
    then the first <img> in the raw (unstripped) body. Feeds vary wildly
    here -- most will have nothing, which is fine, the card just goes text-only."""
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key) or []:
            if m.get("url", "").startswith(("http://", "https://")):
                return m["url"]
    for l in entry.get("links") or []:
        href = l.get("href", "")
        if l.get("rel") == "enclosure" and href.startswith(("http://", "https://")) and (
            str(l.get("type", "")).startswith("image") or IMG_EXT_RE.search(href)
        ):
            return href
    candidates = [entry.get("summary", "")]
    for c in entry.get("content") or []:
        if c.get("value"):
            candidates.append(c["value"])
    for blob in candidates:
        for tag in IMG_TAG_RE.findall(blob or ""):
            src = IMG_SRC_RE.search(tag)
            if not src:
                continue
            url = src.group(1)
            if not url.startswith(("http://", "https://")):
                continue
            # Skip the tracking pixels these feeds open with, by declared size
            # and by the hosts/paths that serve them.
            if IMG_TINY_RE.search(tag) or IMG_PIXEL_HOST_RE.search(url):
                continue
            return url
    return None


def check(name: str, url: str):
    row = {
        "name": name, "url": url, "ok": False, "note": "",
        "entries": 0, "age_hours": None, "median_chars": 0, "dated": 0,
    }
    articles = []

    resp, note = fetch(url)
    if resp is None:
        row["note"] = note
        return row, articles
    if resp.status_code != 200:
        row["note"] = f"HTTP {resp.status_code}"
        return row, articles

    parsed = feedparser.parse(getattr(resp, "_newsdigest_body", b""))
    entries = parsed.entries or []
    bozo_note = ""
    if getattr(parsed, "bozo", 0) and entries:
        bozo_note = f"malformed XML ({type(parsed.get('bozo_exception')).__name__})"
    row["entries"] = len(entries)
    if not entries:
        row["note"] = "0 entries (not a feed? moved?)"
        return row, articles

    bodies = [entry_body(e) for e in entries]
    row["median_chars"] = int(statistics.median(len(b) for b in bodies))

    entry_times = [entry_time(e) for e in entries]
    times = [t for t in entry_times if t]
    row["dated"] = len(times)
    if times:
        row["age_hours"] = round(
            (datetime.now(timezone.utc) - max(times)).total_seconds() / 3600, 1
        )

    row["ok"] = True
    notes = [n for n in (note, bozo_note) if n]
    if resp.url.rstrip("/") != url.rstrip("/"):
        notes.append(f"redirected -> {resp.url}")
    row["note"] = "; ".join(notes)

    for e, body, t in zip(entries, bodies, entry_times):
        title = e.get("title") or "(untitled)"
        snippet = (body[:SNIPPET_LEN] + "…") if len(body) > SNIPPET_LEN else body
        articles.append({
            "source": name,
            "title": title,
            "link": entry_link(e),
            "published": t,
            "snippet": snippet,
            "image": entry_image(e),
            "topic": classify_topic(title, body),
        })
    return row, articles


def verdict(row: dict) -> str:
    if not row["ok"]:
        return "DEAD"
    if row["age_hours"] is None:
        return "NO DATES"
    if row["age_hours"] < -SKEW_HOURS:
        return "FUTURE"          # publisher clock/timezone is wrong
    if row["age_hours"] > STALE_HOURS:
        return "STALE"
    return "OK"


def load_feeds():
    """Loud, readable failures. A mangled YAML paste is the likeliest error."""
    if not FEEDS_FILE.exists():
        raise SystemExit(f"ERROR: {FEEDS_FILE.name} not found at repo root. "
                         "Did the file get committed to the wrong path?")
    try:
        data = yaml.safe_load(FEEDS_FILE.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark else ""
        raise SystemExit(f"ERROR: {FEEDS_FILE.name} is not valid YAML{where}. "
                         "Indentation is usually the culprit after a mobile paste.\n"
                         f"Detail: {exc}")
    if not isinstance(data, dict) or "feeds" not in data:
        raise SystemExit(f"ERROR: {FEEDS_FILE.name} must have a top-level 'feeds:' list.")
    feeds = data["feeds"]
    if not feeds:
        raise SystemExit(f"ERROR: {FEEDS_FILE.name} has no feeds in it.")
    for i, f in enumerate(feeds, 1):
        if not isinstance(f, dict) or not f.get("name") or not f.get("url"):
            raise SystemExit(f"ERROR: feed #{i} is missing 'name' or 'url': {f!r}")
    names = [f["name"] for f in feeds]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SystemExit(f"ERROR: duplicate feed names {sorted(dupes)} — "
                         "these must be unique (sources.name is UNIQUE in the schema).")
    return feeds


def source_hue(name: str) -> int:
    """Deterministic accent hue per source, so the same publisher always
    gets the same badge color across runs."""
    return (sum(ord(c) for c in name) * 47) % 360


def source_initials(name: str) -> str:
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


TITLE_NOISE_RE = re.compile(r"[^\w\s]")


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", TITLE_NOISE_RE.sub(" ", title.lower())).strip()


def dedupe_articles(articles: list) -> list:
    """Collapse near-duplicate headlines (typically the same wire story --
    PTI/ANI/Reuters -- run by multiple publishers) into one card, keeping the
    best-looking representative and listing who else carried it."""
    clusters = []   # each: {"anchor": str, "at": datetime|None, "rep": article, "members": [source]}

    def better(a, b):
        """True if `a` should represent the cluster over current rep `b`."""
        a_img, b_img = bool(a.get("image")), bool(b.get("image"))
        if a_img != b_img:
            return a_img
        if len(a["snippet"]) != len(b["snippet"]):
            return len(a["snippet"]) > len(b["snippet"])
        return (a["published"] or datetime.min.replace(tzinfo=timezone.utc)) > \
               (b["published"] or datetime.min.replace(tzinfo=timezone.utc))

    for a in articles:
        norm = normalize_title(a["title"])
        match = None
        for c in clusters:
            if a["published"] and c["at"]:
                gap_hours = abs((a["published"] - c["at"]).total_seconds()) / 3600
                if gap_hours > DEDUPE_WINDOW_HOURS:
                    continue
            if difflib.SequenceMatcher(None, norm, c["anchor"]).ratio() >= DEDUPE_TITLE_THRESHOLD:
                match = c
                break
        if match is None:
            clusters.append({"anchor": norm, "at": a["published"], "rep": a, "members": [a["source"]]})
            continue
        # `anchor`/`at` deliberately stay pinned to the first article that
        # opened the cluster even when a better-looking rep takes over. Moving
        # them with the rep made membership depend on arrival order: a later
        # headline similar to the original but not to the new rep would start
        # a second card, and one similar only to the new rep would get pulled
        # in transitively -- silently dropping a distinct story.
        if a["source"] not in match["members"]:
            match["members"].append(a["source"])
        if better(a, match["rep"]):
            match["rep"] = a

    out = []
    for c in clusters:
        rep = dict(c["rep"])
        rep["also_from"] = [s for s in c["members"] if s != rep["source"]]
        out.append(rep)
    return out


def render_html(articles: list, sourced_count: int, total_count: int) -> str:
    """Self-contained swipeable article deck -- open reports/index.html (or
    the Pages URL) instead of poking at news.db to see what the feeds have.

    Everything (layout, filtering, the deck, the up-next queue) is built
    client-side from an embedded JSON payload, so all interpolated text has
    to go through the page's esc() and every URL through safeUrl(). See the
    audit note in the README before touching that.
    """
    now = datetime.now(timezone.utc)

    def sort_key(a):
        return a["published"] or datetime.min.replace(tzinfo=timezone.utc)

    payload = [
        {
            # Falls back to source+title because entry_link() returns "" for
            # entries with no <link>, and every one of those would otherwise
            # hash to the same id -- collapsing them in the share deep-link,
            # the saved list, and the click handler's all.find().
            "id": hashlib.sha1(
                (a["link"] or f"{a['source']}\x00{a['title']}").encode("utf-8")
            ).hexdigest()[:10],
            "source": a["source"],
            "title": a["title"],
            "link": a["link"],
            "snippet": a["snippet"],
            "image": a.get("image"),
            "topic": a.get("topic", "General"),
            "published": a["published"].isoformat() if a["published"] else None,
            "hue": source_hue(a["source"]),
            "initials": source_initials(a["source"]),
            "alsoFrom": a.get("also_from", []),
        }
        for a in sorted(articles, key=sort_key, reverse=True)
    ]
    # '</script>' inside a title/snippet would otherwise close the tag early.
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="description" content="A swipeable digest of Indian news, rebuilt every hour.">
<title>newsdigest</title>
<style>
  :root {{
    --bg: #f6f7fb;
    --bg-2: #eceef6;
    --surface: #ffffff;
    --surface-2: #fbfbfe;
    --ink: #10111a;
    --sub: #6a7080;
    --line: rgba(16,17,26,.08);
    --line-2: rgba(16,17,26,.14);
    --accent: #5b5bd6;
    --accent-2: #d6409f;
    --gold: #e8a33d;
    --shadow-sm: 0 1px 2px rgba(16,17,26,.05);
    --shadow-md: 0 2px 6px rgba(16,17,26,.06), 0 12px 24px -14px rgba(16,17,26,.14);
    --shadow-xl: 0 8px 20px -10px rgba(16,17,26,.22), 0 32px 64px -32px rgba(16,17,26,.30);
    --glass: rgba(246,247,251,.72);
    --radius: 22px;
    /* Overshoot for anything that should feel physical; flat-out for the rest. */
    --spring: cubic-bezier(.34, 1.4, .64, 1);
    --out: cubic-bezier(.22, 1, .36, 1);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #08090d;
      --bg-2: #0d0f16;
      --surface: #16181f;
      --surface-2: #1b1e27;
      --ink: #edeef2;
      --sub: #8f96a6;
      --line: rgba(255,255,255,.08);
      --line-2: rgba(255,255,255,.14);
      --accent: #8b8bf0;
      --accent-2: #f472b6;
      --gold: #f0b95c;
      --shadow-sm: 0 1px 2px rgba(0,0,0,.4);
      --shadow-md: 0 2px 6px rgba(0,0,0,.4), 0 12px 24px -14px rgba(0,0,0,.6);
      --shadow-xl: 0 8px 20px -10px rgba(0,0,0,.6), 0 32px 64px -32px rgba(0,0,0,.8);
      --glass: rgba(8,9,13,.72);
    }}
  }}

  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  html, body {{ height: 100%; }}
  body {{
    margin: 0;
    font-family: ui-sans-serif, -apple-system, "SF Pro Text", "Segoe UI Variable Text",
                 "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    background: var(--bg);
    color: var(--ink);
    line-height: 1.45;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
    overflow-x: hidden;
  }}

  /* Slow-drifting colour wash. Fixed + behind everything, never scrolls. */
  .mesh {{
    position: fixed; inset: -20% -10% auto -10%; height: 70vh; z-index: 0;
    pointer-events: none; opacity: .5; filter: blur(60px);
    background:
      radial-gradient(38% 44% at 18% 22%, color-mix(in oklab, var(--accent) 42%, transparent), transparent 70%),
      radial-gradient(34% 40% at 82% 12%, color-mix(in oklab, var(--accent-2) 34%, transparent), transparent 70%),
      radial-gradient(40% 38% at 52% 46%, color-mix(in oklab, var(--gold) 22%, transparent), transparent 72%);
    animation: drift 26s var(--out) infinite alternate;
  }}
  @keyframes drift {{
    from {{ transform: translate3d(-3%, -2%, 0) scale(1); }}
    to   {{ transform: translate3d(4%, 3%, 0) scale(1.12); }}
  }}

  header {{
    position: sticky; top: 0; z-index: 20;
    background: var(--glass);
    backdrop-filter: saturate(1.6) blur(18px);
    -webkit-backdrop-filter: saturate(1.6) blur(18px);
    border-bottom: 1px solid var(--line);
    padding: .85rem 1rem .55rem;
  }}
  .bar {{
    display: flex; align-items: center; justify-content: space-between; gap: 1rem;
    max-width: 1100px; margin: 0 auto;
  }}
  .brand {{
    display: flex; align-items: center; gap: .5rem;
    font-size: 1.06rem; font-weight: 750; letter-spacing: -.028em;
  }}
  .brand .dot {{
    width: .62rem; height: .62rem; border-radius: 50%;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    box-shadow: 0 0 0 0 color-mix(in oklab, var(--accent) 60%, transparent);
    animation: pulse 3.4s var(--out) infinite;
  }}
  @keyframes pulse {{
    0%, 70%, 100% {{ box-shadow: 0 0 0 0 color-mix(in oklab, var(--accent) 55%, transparent); }}
    35% {{ box-shadow: 0 0 0 .42rem transparent; }}
  }}
  .head-right {{ display: flex; align-items: center; gap: .55rem; }}
  .fresh {{ font-size: .74rem; color: var(--sub); font-variant-numeric: tabular-nums; white-space: nowrap; }}
  /* Feed count is nice-to-know, not worth crowding a phone header. */
  @media (max-width: 520px) {{ .fresh.sep, .fresh[title] {{ display: none; }} }}
  .ghost {{
    display: inline-flex; align-items: center; gap: .34rem;
    border: 1px solid var(--line-2); background: var(--surface); color: var(--sub);
    padding: .32rem .62rem; border-radius: 999px;
    font: inherit; font-size: .74rem; font-weight: 650; cursor: pointer;
    transition: transform .18s var(--spring), color .18s, border-color .18s, background .18s;
  }}
  .ghost:hover {{ color: var(--ink); border-color: var(--accent); }}
  .ghost:active {{ transform: scale(.93); }}
  .ghost svg {{ width: .82rem; height: .82rem; }}
  .ghost.spin svg {{ animation: spin .7s var(--out); }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

  .chips {{
    display: flex; gap: .38rem; overflow-x: auto; scrollbar-width: none;
    max-width: 1100px; margin: .6rem auto 0; padding-bottom: .18rem;
    -webkit-overflow-scrolling: touch; scroll-snap-type: x proximity;
  }}
  .chips::-webkit-scrollbar {{ display: none; }}
  .chips-sub {{ margin-top: .34rem; }}
  .chip {{
    flex: none; scroll-snap-align: start;
    border: 1px solid var(--line-2); background: var(--surface); color: var(--sub);
    padding: .32rem .68rem; border-radius: 999px;
    font: inherit; font-size: .755rem; font-weight: 650; white-space: nowrap; cursor: pointer;
    transition: transform .2s var(--spring), background .2s var(--out), color .2s, border-color .2s;
    animation: chipIn .4s var(--out) both;
    animation-delay: calc(var(--i, 0) * 22ms);
  }}
  @keyframes chipIn {{
    from {{ opacity: 0; transform: translateY(6px) scale(.94); }}
    to   {{ opacity: 1; transform: none; }}
  }}
  .chip .n {{ opacity: .55; font-weight: 550; margin-left: .1rem; }}
  .chip:hover {{ color: var(--ink); border-color: var(--line-2); }}
  .chip:active {{ transform: scale(.94); }}
  .chip.on {{
    color: #fff; border-color: transparent;
    background: linear-gradient(135deg,
      hsl(var(--hue, 248) 62% 54%), hsl(calc(var(--hue, 248) + 26) 66% 48%));
    box-shadow: 0 2px 10px -4px hsl(var(--hue, 248) 62% 54% / .7);
  }}
  .chip.on .n {{ opacity: .8; }}
  .chip[data-f="__saved__"].on {{
    background: linear-gradient(135deg, var(--gold), #d98324);
    box-shadow: 0 2px 10px -4px color-mix(in oklab, var(--gold) 70%, transparent);
  }}

  .rail {{ max-width: 1100px; margin: .6rem auto 0; height: 2px; background: var(--line); border-radius: 2px; }}
  .rail i {{
    display: block; height: 100%; width: 0; border-radius: 2px;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    transition: width .45s var(--out);
  }}

  .layout {{
    position: relative; z-index: 1;
    max-width: 1100px; margin: 0 auto; padding: 1.25rem 1rem 2.5rem;
    display: grid; grid-template-columns: 1fr; gap: 1.5rem; align-items: start;
  }}

  .stage {{
    position: relative;
    height: clamp(420px, 62vh, 560px);
    perspective: 1400px;
  }}

  .card {{
    position: absolute; inset: 0; margin: auto;
    width: min(94%, 400px); height: 100%;
    display: flex; flex-direction: column; overflow: hidden;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); box-shadow: var(--shadow-xl);
    transform-origin: 50% 100%;
    will-change: transform, opacity;
    animation: cardIn .5s var(--out) both;
    animation-delay: calc(var(--i, 0) * 55ms);
  }}
  @keyframes cardIn {{
    from {{ opacity: 0; transform: translateY(14px) scale(.96); }}
  }}
  .card.top {{ cursor: grab; touch-action: pan-y; }}
  .card.top:active {{ cursor: grabbing; }}
  .card.drag {{ transition: none !important; }}
  .card:not(.drag) {{ transition: transform .42s var(--spring), opacity .3s var(--out), box-shadow .3s; }}

  .media {{ position: relative; flex: none; height: 42%; background: var(--bg-2); overflow: hidden; }}
  .media img {{
    width: 100%; height: 100%; object-fit: cover; display: block;
    opacity: 0; transform: scale(1.06);
    transition: opacity .55s var(--out), transform 1.1s var(--out);
  }}
  .media img.in {{ opacity: 1; transform: none; }}
  /* Shimmer sits under the image and is simply covered once it paints. */
  .media::before {{
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(100deg, var(--bg-2) 20%, var(--surface-2) 40%, var(--bg-2) 60%);
    background-size: 220% 100%;
    animation: shimmer 1.5s linear infinite;
  }}
  .media.done::before {{ display: none; }}
  @keyframes shimmer {{ to {{ background-position: -220% 0; }} }}
  .media .scrim {{
    position: absolute; inset: auto 0 0 0; height: 55%;
    background: linear-gradient(to top, var(--surface), transparent);
    pointer-events: none;
  }}

  .body {{ flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 1.05rem 1.15rem 1.1rem; }}
  .metarow {{ display: flex; align-items: center; gap: .4rem; flex-wrap: wrap; margin-bottom: .55rem; }}
  .src {{
    display: inline-flex; align-items: center; gap: .38rem;
    font-size: .735rem; font-weight: 700; letter-spacing: -.005em;
    color: hsl(var(--hue) 52% 40%);
  }}
  @media (prefers-color-scheme: dark) {{ .src {{ color: hsl(var(--hue) 72% 74%); }} }}
  .ava {{
    width: 1.4rem; height: 1.4rem; border-radius: 50%; flex: none;
    display: grid; place-items: center;
    font-size: .58rem; font-weight: 800; letter-spacing: -.02em;
    color: #fff;
    background: linear-gradient(135deg, hsl(var(--hue) 62% 56%), hsl(calc(var(--hue) + 30) 62% 46%));
  }}
  .pill {{
    font-size: .66rem; font-weight: 700; letter-spacing: .03em; text-transform: uppercase;
    color: var(--sub); background: var(--bg-2);
    border: 1px solid var(--line); padding: .16rem .46rem; border-radius: 999px;
  }}
  .card h2 {{
    margin: 0 0 .4rem; font-size: 1.19rem; line-height: 1.28;
    font-weight: 760; letter-spacing: -.022em;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }}
  .also {{ font-size: .72rem; color: var(--sub); margin-bottom: .45rem; }}
  .also b {{ color: var(--ink); font-weight: 650; }}
  .snip {{
    margin: 0; color: var(--sub); font-size: .875rem; line-height: 1.52; flex: 1; min-height: 0;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }}
  .foot {{
    display: flex; align-items: center; justify-content: space-between; gap: .6rem;
    margin-top: .85rem; padding-top: .7rem; border-top: 1px solid var(--line);
  }}
  .foot time {{ font-size: .73rem; color: var(--sub); font-variant-numeric: tabular-nums; }}
  .read {{
    display: inline-flex; align-items: center; gap: .3rem;
    font-size: .8rem; font-weight: 700; color: var(--accent); text-decoration: none;
    transition: gap .2s var(--spring);
  }}
  .read svg {{ width: .85rem; height: .85rem; }}
  .read:hover {{ gap: .5rem; text-decoration: underline; }}

  .tools {{ position: absolute; top: .7rem; right: .7rem; display: flex; gap: .35rem; z-index: 3; }}
  .tool {{
    width: 2.1rem; height: 2.1rem; border-radius: 50%; display: grid; place-items: center;
    border: 1px solid transparent; cursor: pointer; color: var(--ink);
    background: color-mix(in oklab, var(--surface) 78%, transparent);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    box-shadow: var(--shadow-sm);
    transition: transform .2s var(--spring), color .2s, background .2s;
  }}
  .tool svg {{ width: 1rem; height: 1rem; }}
  .tool:hover {{ transform: scale(1.08); }}
  .tool:active {{ transform: scale(.88); }}
  .tool.on {{ color: var(--gold); }}
  .tool.on svg {{ fill: var(--gold); }}
  .tool.pop {{ animation: pop .42s var(--spring); }}
  @keyframes pop {{
    0% {{ transform: scale(1); }}
    38% {{ transform: scale(1.34) rotate(9deg); }}
    100% {{ transform: scale(1); }}
  }}

  .end {{ align-items: center; justify-content: center; text-align: center; padding: 2rem 1.4rem; cursor: default; }}
  .end .big {{
    font-size: 2.4rem; line-height: 1; margin-bottom: .6rem;
    animation: cardIn .5s var(--spring) both .1s;
  }}
  .end h2 {{ -webkit-line-clamp: unset; margin-bottom: .3rem; }}
  .end p {{ color: var(--sub); font-size: .88rem; margin: 0 0 1rem; }}

  .ctrls {{ display: flex; align-items: center; justify-content: center; gap: 1rem; margin-top: 1.15rem; }}
  .rnd {{
    width: 3.15rem; height: 3.15rem; border-radius: 50%; display: grid; place-items: center;
    border: 1px solid var(--line-2); background: var(--surface); color: var(--ink);
    box-shadow: var(--shadow-md); cursor: pointer;
    transition: transform .22s var(--spring), border-color .2s, color .2s;
  }}
  .rnd svg {{ width: 1.15rem; height: 1.15rem; }}
  .rnd:hover:not(:disabled) {{ transform: translateY(-2px) scale(1.05); border-color: var(--accent); color: var(--accent); }}
  .rnd:active:not(:disabled) {{ transform: scale(.9); }}
  .rnd:disabled {{ opacity: .35; cursor: default; }}
  .count {{ text-align: center; color: var(--sub); font-size: .78rem; margin-top: .7rem; font-variant-numeric: tabular-nums; }}
  .hint {{ display: none; text-align: center; color: var(--sub); font-size: .74rem; margin-top: .3rem; opacity: .75; }}
  .hint kbd {{
    font: inherit; font-size: .7rem; padding: .04rem .3rem; border-radius: 5px;
    border: 1px solid var(--line-2); background: var(--surface);
  }}

  .queue {{ display: none; }}
  .queue h3 {{
    font-size: .68rem; font-weight: 750; letter-spacing: .09em; text-transform: uppercase;
    color: var(--sub); margin: 0 0 .55rem;
  }}
  .qi {{
    display: flex; gap: .6rem; align-items: flex-start; width: 100%; text-align: left;
    padding: .55rem .6rem; border-radius: 13px; border: 1px solid transparent;
    background: none; font: inherit; color: inherit; cursor: pointer;
    animation: chipIn .4s var(--out) both; animation-delay: calc(var(--i, 0) * 40ms);
    transition: background .16s, border-color .16s, transform .16s var(--out);
  }}
  .qi:hover {{ background: var(--surface); border-color: var(--line); transform: translateX(2px); }}
  .qi .bar {{ flex: none; width: 3px; align-self: stretch; border-radius: 3px; background: hsl(var(--hue) 62% 56%); }}
  .qi .t {{ font-size: .81rem; font-weight: 640; line-height: 1.34; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
  .qi .s {{ font-size: .7rem; color: var(--sub); margin-top: .12rem; }}
  .qempty {{ color: var(--sub); font-size: .8rem; }}

  .toast {{
    position: fixed; left: 50%; bottom: 1.4rem; z-index: 60;
    transform: translate(-50%, 24px) scale(.96); opacity: 0; pointer-events: none;
    display: flex; align-items: center; gap: .45rem;
    padding: .6rem 1rem; border-radius: 999px;
    background: color-mix(in oklab, var(--ink) 92%, transparent); color: var(--bg);
    font-size: .82rem; font-weight: 600; box-shadow: var(--shadow-xl);
    backdrop-filter: blur(10px);
    transition: opacity .28s var(--out), transform .38s var(--spring);
  }}
  .toast.show {{ opacity: 1; transform: translate(-50%, 0) scale(1); }}

  :focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 8px; }}

  @media (min-width: 900px) {{
    .layout {{ grid-template-columns: minmax(0, 1fr) 296px; gap: 2.25rem; }}
    .stage {{ height: clamp(460px, 64vh, 600px); }}
    .card {{ width: min(88%, 430px); }}
    .queue {{ display: block; position: sticky; top: 8.5rem; }}
    .hint {{ display: block; }}
  }}

  @media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{
      animation-duration: .001ms !important; animation-iteration-count: 1 !important;
      transition-duration: .001ms !important;
    }}
    .mesh {{ animation: none; }}
  }}
</style>
</head>
<body data-generated="{now.isoformat()}">
  <div class="mesh" aria-hidden="true"></div>

  <header>
    <div class="bar">
      <div class="brand"><span class="dot" aria-hidden="true"></span> newsdigest</div>
      <div class="head-right">
        <span class="fresh" title="{sourced_count} of {total_count} feeds contributed at least one article to this deck. Feed-by-feed health lives in reports/feed_check.md.">{sourced_count}/{total_count} feeds</span>
        <span class="fresh sep" aria-hidden="true">&middot;</span>
        <span class="fresh" id="fresh"></span>
        <button class="ghost" id="refresh" title="Check for a newer snapshot">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/>
          </svg>
          Refresh
        </button>
      </div>
    </div>
    <nav class="chips" id="topics" aria-label="Filter by topic"></nav>
    <nav class="chips chips-sub" id="sources" aria-label="Filter by source"></nav>
    <div class="rail"><i id="rail"></i></div>
  </header>

  <div class="layout">
    <div>
      <main class="stage" id="stage" aria-live="polite"></main>
      <div class="ctrls">
        <button class="rnd" id="prev" title="Previous (left arrow)" aria-label="Previous article">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg>
        </button>
        <button class="rnd" id="next" title="Next (right arrow)" aria-label="Next article">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18l6-6-6-6"/></svg>
        </button>
      </div>
      <div class="count" id="count"></div>
      <div class="hint">
        <kbd>&larr;</kbd> <kbd>&rarr;</kbd> to move &middot; <kbd>S</kbd> save &middot; drag the card either way
      </div>
    </div>
    <aside class="queue" id="queue">
      <h3>Up next</h3>
      <div id="qlist"></div>
    </aside>
  </div>

  <div class="toast" id="toast" role="status" aria-live="polite"></div>

  <script id="data" type="application/json">{data_json}</script>
  <script>
  (function () {{
    "use strict";

    var ICON = {{
      star: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.2l2.85 5.78 6.38.93-4.61 4.5 1.09 6.36L12 17.76l-5.71 3.01 1.09-6.36-4.61-4.5 6.38-.93L12 3.2z"/></svg>',
      share: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v7a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-7"/><path d="M12 15V3"/><path d="M8 7l4-4 4 4"/></svg>',
      out: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17L17 7"/><path d="M8 7h9v9"/></svg>'
    }};

    var all = JSON.parse(document.getElementById('data').textContent);
    var stage = document.getElementById('stage');
    var topicsEl = document.getElementById('topics');
    var sourcesEl = document.getElementById('sources');
    var qlist = document.getElementById('qlist');
    var countEl = document.getElementById('count');
    var railEl = document.getElementById('rail');
    var freshEl = document.getElementById('fresh');
    var toastEl = document.getElementById('toast');
    var prevBtn = document.getElementById('prev');
    var nextBtn = document.getElementById('next');

    var SAVED_KEY = 'newsdigest:saved';
    var saved = new Set();
    try {{
      var raw = JSON.parse(localStorage.getItem(SAVED_KEY) || '[]');
      if (Array.isArray(raw)) saved = new Set(raw);
    }} catch (e) {{}}

    var sources = Array.from(new Set(all.map(function (a) {{ return a.source; }}))).sort();
    var topics = Array.from(new Set(all.map(function (a) {{ return a.topic; }}))).sort();
    var hueOf = {{}};
    all.forEach(function (a) {{ if (!(a.source in hueOf)) hueOf[a.source] = a.hue; }});

    // Saved ids accumulate across hourly rebuilds, but only ids still in this
    // snapshot can ever be shown -- so the chip counted articles the Saved
    // view could not display. Prune to what exists, and only rewrite storage
    // if something actually dropped (keeps other tabs' entries intact).
    (function pruneSaved() {{
      var live = new Set(all.map(function (a) {{ return a.id; }}));
      var kept = Array.from(saved).filter(function (id) {{ return live.has(id); }});
      if (kept.length !== saved.size) {{
        saved = new Set(kept);
        try {{ localStorage.setItem(SAVED_KEY, JSON.stringify(kept)); }} catch (e) {{}}
      }}
    }})();

    var sourceFilter = 'all';
    var topicFilter = 'all';
    var index = 0;
    var busy = false;   // an exit animation owns the deck; ignore new input
    var renderSeq = 0;  // bumped per render so a finished fly-out can tell if
                        // the deck moved on under it (filter click mid-swipe)

    function esc(s) {{
      return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {{
        return {{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }}[c];
      }});
    }}
    // Anything that isn't plain http(s) is dropped, so a hostile feed can't
    // smuggle a javascript: or data: URI into an href or img src.
    function safeUrl(u) {{ return /^https?:\\/\\//i.test(u || '') ? u : ''; }}

    function filtered() {{
      var list = sourceFilter === '__saved__'
        ? all.filter(function (a) {{ return saved.has(a.id); }})
        : sourceFilter === 'all' ? all : all.filter(function (a) {{ return a.source === sourceFilter; }});
      if (topicFilter !== 'all') list = list.filter(function (a) {{ return a.topic === topicFilter; }});
      return list;
    }}

    // index === length is legal: that's the "all caught up" card.
    function clampIndex() {{
      var n = filtered().length;
      if (index > n) index = n;
      if (index < 0) index = 0;
    }}

    function relTime(iso) {{
      if (!iso) return 'undated';
      var mins = (Date.now() - new Date(iso).getTime()) / 60000;
      if (mins < 0) mins = 0;
      if (mins < 1) return 'just now';
      if (mins < 60) return Math.floor(mins) + 'm ago';
      var h = mins / 60;
      if (h < 24) return Math.floor(h) + 'h ago';
      var d = h / 24;
      if (d < 7) return Math.floor(d) + 'd ago';
      return new Date(iso).toLocaleDateString(undefined, {{ month: 'short', day: 'numeric' }});
    }}

    function toast(msg) {{
      toastEl.textContent = msg;
      toastEl.classList.add('show');
      clearTimeout(toast.t);
      toast.t = setTimeout(function () {{ toastEl.classList.remove('show'); }}, 1900);
    }}

    /* ---------- filter chips ---------- */

    function chip(label, attr, val, on, hue, i) {{
      return '<button class="chip' + (on ? ' on' : '') + '" ' + attr + '="' + esc(val) + '"'
        + ' style="--i:' + i + (hue === undefined ? '' : ';--hue:' + hue) + '"'
        + ' aria-pressed="' + (on ? 'true' : 'false') + '">' + label + '</button>';
    }}

    function renderTopics() {{
      var h = chip('All topics', 'data-t', 'all', topicFilter === 'all', undefined, 0);
      topics.forEach(function (t, i) {{
        var n = all.filter(function (a) {{ return a.topic === t; }}).length;
        h += chip(esc(t) + ' <span class="n">' + n + '</span>', 'data-t', t, topicFilter === t, undefined, i + 1);
      }});
      topicsEl.innerHTML = h;
    }}

    function renderSources() {{
      var h = chip('All', 'data-f', 'all', sourceFilter === 'all', undefined, 0);
      h += chip(ICON.star + ' ' + saved.size, 'data-f', '__saved__', sourceFilter === '__saved__', undefined, 1);
      sources.forEach(function (s, i) {{
        var n = all.filter(function (a) {{ return a.source === s; }}).length;
        h += chip(esc(s) + ' <span class="n">' + n + '</span>', 'data-f', s, sourceFilter === s, hueOf[s], i + 2);
      }});
      sourcesEl.innerHTML = h;
      // The star glyph inside a chip must not swallow the click target.
      sourcesEl.querySelectorAll('.chip svg').forEach(function (s) {{
        s.style.width = '.72rem'; s.style.height = '.72rem'; s.style.verticalAlign = '-.1em';
      }});
    }}

    /* ---------- cards ---------- */

    function cardMarkup(a) {{
      var img = a.image ? safeUrl(a.image) : '';
      var media = img
        ? '<div class="media"><img alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" src="' + esc(img) + '"><div class="scrim"></div></div>'
        : '';
      var also = (a.alsoFrom && a.alsoFrom.length)
        ? '<div class="also">also on <b>' + a.alsoFrom.map(esc).join('</b>, <b>') + '</b></div>'
        : '';
      var on = saved.has(a.id);
      var link = safeUrl(a.link);
      return ''
        + '<div class="tools">'
        +   '<button class="tool save' + (on ? ' on' : '') + '" data-act="save" title="Save for later"'
        +   ' aria-label="Save for later" aria-pressed="' + (on ? 'true' : 'false') + '">' + ICON.star + '</button>'
        +   '<button class="tool" data-act="share" title="Share" aria-label="Share">' + ICON.share + '</button>'
        + '</div>'
        + media
        + '<div class="body">'
        +   '<div class="metarow">'
        +     '<span class="src"><span class="ava">' + esc(a.initials) + '</span>' + esc(a.source) + '</span>'
        +     '<span class="pill">' + esc(a.topic) + '</span>'
        +   '</div>'
        +   '<h2>' + esc(a.title) + '</h2>'
        +   also
        +   '<p class="snip">' + esc(a.snippet) + '</p>'
        +   '<div class="foot"><time>' + esc(relTime(a.published)) + '</time>'
        +     (link ? '<a class="read" href="' + esc(link) + '" target="_blank" rel="noopener noreferrer">Read' + ICON.out + '</a>' : '')
        +   '</div>'
        + '</div>';
    }}

    function render() {{
      var list = filtered();
      clampIndex();
      stage.innerHTML = '';
      busy = false;
      renderSeq++;

      var pct = list.length ? Math.min(100, (index / list.length) * 100) : 0;
      railEl.style.width = pct + '%';
      prevBtn.disabled = index === 0;
      nextBtn.disabled = index >= list.length;

      if (index >= list.length) {{
        countEl.textContent = list.length ? 'End of the queue' : 'Nothing matches those filters';
        var end = document.createElement('article');
        end.className = 'card end';
        end.innerHTML = list.length
          ? '<div class="big">&#10003;</div><h2>All caught up</h2><p>You have been through every story in this view.</p>'
            + '<button class="ghost" data-act="restart">Start over</button>'
          : '<div class="big">&#9788;</div><h2>Nothing here</h2><p>Try a different topic or source.</p>';
        stage.appendChild(end);
        renderQueue();
        return;
      }}

      countEl.textContent = (index + 1) + ' of ' + list.length;

      // Paint back-to-front so the top card is last in the DOM.
      var depth = Math.min(3, list.length - index);
      for (var i = depth - 1; i >= 0; i--) {{
        var a = list[index + i];
        var el = document.createElement('article');
        el.className = 'card' + (i === 0 ? ' top' : '');
        el.style.setProperty('--hue', a.hue);
        el.style.setProperty('--i', depth - 1 - i);
        el.style.zIndex = String(10 - i);
        el.dataset.id = a.id;
        el.innerHTML = cardMarkup(a);
        if (i > 0) {{
          el.style.transform = 'translateY(' + (i * 11) + 'px) scale(' + (1 - i * 0.045) + ')';
          el.style.opacity = i === 2 ? '.55' : '.85';
          el.setAttribute('aria-hidden', 'true');
        }} else {{
          attachDrag(el);
        }}
        var im = el.querySelector('.media img');
        if (im) bindImage(im);
        stage.appendChild(el);
      }}
      renderQueue();
    }}

    // Fade the image in when it lands; drop the whole media block if the
    // publisher's CDN 404s so we never show a broken-image box.
    function bindImage(im) {{
      var media = im.parentNode;
      function ok() {{ media.classList.add('done'); im.classList.add('in'); }}
      if (im.complete && im.naturalWidth > 0) {{ ok(); return; }}
      im.addEventListener('load', ok);
      im.addEventListener('error', function () {{ media.remove(); }});
    }}

    function renderQueue() {{
      var rest = filtered().slice(index + 1, index + 7);
      if (!rest.length) {{
        qlist.innerHTML = '<p class="qempty">Nothing queued.</p>';
        return;
      }}
      qlist.innerHTML = rest.map(function (a, i) {{
        return '<button class="qi" data-jump="' + esc(a.id) + '" style="--hue:' + a.hue + ';--i:' + i + '">'
          + '<span class="bar"></span><span><span class="t">' + esc(a.title) + '</span>'
          + '<span class="s">' + esc(a.source) + ' &middot; ' + esc(relTime(a.published)) + '</span></span></button>';
      }}).join('');
    }}

    /* ---------- navigation ---------- */

    function advance() {{ index += 1; render(); }}
    function goBack() {{ if (index > 0) {{ index -= 1; render(); }} }}

    var DUR = 300;
    var reduceMotion = window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function flyOut(card, dir) {{
      if (busy) return;
      busy = true;
      var seq = renderSeq;

      // If a filter click (or a deep link) re-rendered while the card was
      // flying out, that render already chose the index -- advancing again
      // here would silently skip the first article of the new view.
      var spent = false;
      function done() {{
        if (spent) return;
        spent = true;
        if (seq === renderSeq) advance();
        else busy = false;
      }}

      if (reduceMotion || typeof card.animate !== 'function') {{ done(); return; }}

      var anim = card.animate([
        {{ transform: card.style.transform || 'none', opacity: 1 }},
        {{ transform: 'translate(' + (dir * 640) + 'px, 40px) rotate(' + (dir * 22) + 'deg)', opacity: 0 }}
      ], {{ duration: DUR, easing: 'cubic-bezier(.22,1,.36,1)', fill: 'forwards' }});

      if (anim.finished && anim.finished.then) anim.finished.then(done, done);
      else anim.onfinish = done;

      // Watchdog. A hidden or throttled tab never composites, so the
      // animation never finishes and `finished` never settles -- without this
      // `busy` would stay true and the deck would wedge for good. Verified:
      // background the tab mid-swipe and every later tap/arrow is swallowed.
      setTimeout(done, DUR + 150);
    }}

    // Dragging either direction just advances -- there is no like/dislike
    // here. Opening an article is only ever the explicit Read link.
    function attachDrag(card) {{
      var x0 = 0, y0 = 0, dx = 0, dy = 0, active = false, pid = null;
      var THRESH = 96;

      card.addEventListener('pointerdown', function (e) {{
        if (busy || e.button !== 0) return;
        if (e.target.closest('button, a')) return;
        active = true; pid = e.pointerId; dx = 0; dy = 0;
        x0 = e.clientX; y0 = e.clientY;
        card.classList.add('drag');
        try {{ card.setPointerCapture(pid); }} catch (err) {{}}
      }});

      card.addEventListener('pointermove', function (e) {{
        if (!active || e.pointerId !== pid) return;
        dx = e.clientX - x0;
        dy = e.clientY - y0;
        var lift = Math.min(Math.abs(dx) / 900, .04);
        card.style.transform = 'translate(' + dx + 'px,' + (dy * .35) + 'px)'
          + ' rotate(' + (dx / 22) + 'deg) scale(' + (1 + lift) + ')';
        card.style.opacity = String(Math.max(.45, 1 - Math.abs(dx) / 640));
      }});

      function release(e) {{
        if (!active || (e && e.pointerId !== pid)) return;
        active = false;
        try {{ card.releasePointerCapture(pid); }} catch (err) {{}}
        card.classList.remove('drag');
        if (Math.abs(dx) > THRESH) {{
          flyOut(card, dx > 0 ? 1 : -1);
        }} else {{
          card.style.transform = '';
          card.style.opacity = '';
        }}
      }}
      card.addEventListener('pointerup', release);
      card.addEventListener('pointercancel', release);
    }}

    /* ---------- actions ---------- */

    function toggleSave(id, btn) {{
      if (saved.has(id)) saved.delete(id); else saved.add(id);
      try {{ localStorage.setItem(SAVED_KEY, JSON.stringify(Array.from(saved))); }} catch (e) {{}}
      var on = saved.has(id);
      if (btn) {{
        btn.classList.toggle('on', on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        btn.classList.remove('pop');
        void btn.offsetWidth;          // restart the keyframe
        btn.classList.add('pop');
      }}
      renderSources();
      toast(on ? 'Saved' : 'Removed from saved');
      // Un-saving inside the Saved view shrinks the list under our feet.
      if (sourceFilter === '__saved__') render();
    }}

    function share(a) {{
      var url = location.origin + location.pathname + '#' + a.id;
      var text = a.title + ' — via newsdigest';
      if (navigator.share) {{
        navigator.share({{ title: a.title, text: text, url: url }}).catch(function () {{}});
      }} else if (navigator.clipboard) {{
        navigator.clipboard.writeText(text + '\\n' + url)
          .then(function () {{ toast('Link copied'); }}, function () {{ toast('Could not copy'); }});
      }} else {{
        toast(url);
      }}
    }}

    stage.addEventListener('click', function (e) {{
      var btn = e.target.closest('[data-act]');
      if (!btn) return;
      var act = btn.dataset.act;
      if (act === 'restart') {{ index = 0; render(); return; }}
      var card = btn.closest('.card');
      if (!card) return;
      var a = all.find(function (x) {{ return x.id === card.dataset.id; }});
      if (!a) return;
      if (act === 'save') toggleSave(a.id, btn);
      else if (act === 'share') share(a);
    }});

    qlist.addEventListener('click', function (e) {{
      var btn = e.target.closest('[data-jump]');
      if (!btn) return;
      var list = filtered();
      var pos = list.findIndex(function (a) {{ return a.id === btn.dataset.jump; }});
      if (pos >= 0) {{ index = pos; render(); }}
    }});

    topicsEl.addEventListener('click', function (e) {{
      var btn = e.target.closest('.chip');
      if (!btn) return;
      topicFilter = btn.dataset.t;
      index = 0;
      renderTopics();
      render();
    }});

    sourcesEl.addEventListener('click', function (e) {{
      var btn = e.target.closest('.chip');
      if (!btn) return;
      sourceFilter = btn.dataset.f;
      index = 0;
      renderSources();
      render();
    }});

    prevBtn.addEventListener('click', goBack);
    nextBtn.addEventListener('click', function () {{
      var top = stage.querySelector('.card.top');
      if (top) flyOut(top, 1); else advance();
    }});

    document.addEventListener('keydown', function (e) {{
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      var t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
      if (e.key === 'ArrowRight') {{
        e.preventDefault();
        var top = stage.querySelector('.card.top');
        if (top) flyOut(top, 1); else advance();
      }} else if (e.key === 'ArrowLeft') {{
        e.preventDefault(); goBack();
      }} else if (e.key === 's' || e.key === 'S') {{
        var c = stage.querySelector('.card.top');
        if (c) toggleSave(c.dataset.id, c.querySelector('.save'));
      }}
    }});

    /* ---------- deep links ---------- */

    function jumpToHash() {{
      var id = location.hash.slice(1);
      if (!id) return false;
      // Clear filters first: a shared card may not be in the current view.
      if (!all.some(function (a) {{ return a.id === id; }})) return false;
      sourceFilter = 'all'; topicFilter = 'all';
      index = filtered().findIndex(function (a) {{ return a.id === id; }});
      if (index < 0) index = 0;
      renderTopics(); renderSources(); render();
      return true;
    }}
    window.addEventListener('hashchange', jumpToHash);

    /* ---------- freshness / refresh ---------- */

    var COOLDOWN_MIN = 30;
    var built = new Date(document.body.dataset.generated);

    function minsOld() {{ return (Date.now() - built.getTime()) / 60000; }}

    function paintFresh() {{
      var m = Math.floor(minsOld());
      if (isNaN(m)) {{ freshEl.textContent = ''; return; }}
      freshEl.textContent = 'Updated ' + (m < 1 ? 'just now' : m < 60 ? m + 'm ago' : Math.floor(m / 60) + 'h ago');
    }}
    paintFresh();
    setInterval(paintFresh, 60000);

    // This does NOT kick off a new Actions run -- a static page has nowhere
    // safe to keep a token that could. It re-fetches whatever the hourly
    // cron last published, past any stale browser/CDN copy.
    document.getElementById('refresh').addEventListener('click', function () {{
      var btn = this;
      btn.classList.remove('spin'); void btn.offsetWidth; btn.classList.add('spin');
      var m = minsOld();
      if (!isNaN(m) && m < COOLDOWN_MIN) {{
        toast('Already fresh — built ' + Math.floor(m) + 'm ago');
        return;
      }}
      toast('Fetching the latest…');
      setTimeout(function () {{
        location.replace(location.pathname + '?t=' + Date.now() + location.hash);
      }}, 420);
    }});

    /* ---------- boot ---------- */

    renderTopics();
    renderSources();
    if (!jumpToHash()) render();
  }})();
  </script>
</body>
</html>
"""


def main() -> int:
    feeds = load_feeds()
    rows = []
    all_articles = []
    for f in feeds:
        print(f"checking {f['name']} ...", flush=True)
        r, arts = check(f["name"], f["url"])
        r["verdict"] = verdict(r)
        print(f"   {r['verdict']} {r['note']}".rstrip(), flush=True)
        rows.append(r)
        if r["ok"]:
            all_articles.extend(arts)
        time.sleep(1)  # be polite

    live = [r for r in rows if r["verdict"] == "OK"]
    stubs = [r for r in live if r["median_chars"] < STUB_THRESHOLD]
    total_items = sum(r["entries"] for r in live)

    out = [
        "# Feed check",
        "",
        f"Run: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        "",
        f"- **{len(live)} / {len(rows)} feeds usable**",
        f"- {total_items} items visible right now across live feeds",
        f"- {len(stubs)} of {len(live)} live feeds are teaser-only "
        f"(<{STUB_THRESHOLD} chars) -> need article extraction",
        "",
        f"Legend: OK / FUTURE (publisher clock wrong) / STALE (>{STALE_HOURS}h) / NO DATES / DEAD",
        "",
    ]

    for group in ("OK", "FUTURE", "STALE", "NO DATES", "DEAD"):
        chunk = [r for r in rows if r["verdict"] == group]
        if not chunk:
            continue
        out += [f"## {group} ({len(chunk)})", ""]
        for r in sorted(chunk, key=lambda x: x["name"]):
            bits = []
            if r["ok"]:
                bits.append(f"{r['entries']} items")
                if r["age_hours"] is not None:
                    bits.append(f"newest {r['age_hours']}h ago")
                bits.append(f"median body {r['median_chars']} chars")
                bits.append("FULL TEXT" if r["median_chars"] >= STUB_THRESHOLD else "teaser")
                if r["dated"] < r["entries"]:
                    bits.append(f"only {r['dated']}/{r['entries']} dated")
            if r["note"]:
                bits.append(r["note"])
            out += [f"**{r['name']}** — {', '.join(bits)}", f"`{r['url']}`", ""]

    # encoding="utf-8" on every write, explicitly. Without it Python uses the
    # platform default -- cp1252 on Windows -- and a single '₹' in a real
    # headline aborts the whole run with UnicodeEncodeError. The Actions runner
    # happens to default to UTF-8, so this only ever bit local runs, silently
    # making the script look Linux-only. The HTML declares UTF-8 anyway.
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(out), encoding="utf-8")
    REPORT_JSON.write_text(json.dumps(
        {"checked_at": datetime.now(timezone.utc).isoformat(), "feeds": rows},
        indent=2, ensure_ascii=False,
    ), encoding="utf-8")
    deduped = dedupe_articles(all_articles)
    merged = len(all_articles) - len(deduped)

    # Newest first, then cap. Undated entries sort last rather than winning the
    # top of the deck by accident.
    deduped.sort(
        key=lambda a: a["published"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    dropped = max(0, len(deduped) - DECK_LIMIT)
    deck = deduped[:DECK_LIMIT]

    # Count the feeds that actually put an article in the deck, not the ones
    # that merely graded OK: all_articles takes anything with r["ok"], which
    # includes STALE/FUTURE/NO DATES, so len(live) would understate the deck.
    sourced = len({a["source"] for a in deck})
    REPORT_HTML.write_text(render_html(deck, sourced, len(rows)), encoding="utf-8")

    print(f"\nwrote {REPORT_MD.name} + {REPORT_JSON.name} + {REPORT_HTML.name}")
    print(f"  {len(live)}/{len(rows)} feeds graded OK, {sourced} contributed to the deck")
    print(f"  {len(all_articles)} articles -> {len(deduped)} after merging "
          f"{merged} cross-agency duplicate{'s' if merged != 1 else ''}")
    if dropped:
        print(f"  deck capped at {DECK_LIMIT}: {dropped} older card"
              f"{'s' if dropped != 1 else ''} not shown (raise DECK_LIMIT to include them)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
