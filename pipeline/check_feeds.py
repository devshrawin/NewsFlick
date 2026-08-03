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
IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

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
        "artificial intelligence", " ai ", "smartphone", "app launch",
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
        "doctor", "outbreak", "who ", "medical",
    ],
}


def classify_topic(title: str, snippet: str) -> str:
    text = f" {title.lower()} {snippet.lower()} "
    best_topic, best_hits = "General", 0
    for topic, keywords in TOPIC_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in text)
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
        m = IMG_TAG_RE.search(blob or "")
        if m and m.group(1).startswith(("http://", "https://")):
            return m.group(1)
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
        data = yaml.safe_load(FEEDS_FILE.read_text())
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
    clusters = []   # each: {"rep": article, "norm": str, "also_from": [source, ...]}

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
            if a["published"] and c["rep"]["published"]:
                gap_hours = abs((a["published"] - c["rep"]["published"]).total_seconds()) / 3600
                if gap_hours > DEDUPE_WINDOW_HOURS:
                    continue
            if difflib.SequenceMatcher(None, norm, c["norm"]).ratio() >= DEDUPE_TITLE_THRESHOLD:
                match = c
                break
        if match is None:
            clusters.append({"rep": a, "norm": norm, "also_from": []})
            continue
        old_rep = match["rep"]
        if better(a, old_rep):
            match["rep"], match["norm"] = a, norm
            demoted_source = old_rep["source"]
        else:
            demoted_source = a["source"]
        if demoted_source != match["rep"]["source"] and demoted_source not in match["also_from"]:
            match["also_from"].append(demoted_source)

    out = []
    for c in clusters:
        rep = dict(c["rep"])
        rep["also_from"] = c["also_from"]
        out.append(rep)
    return out


def render_html(articles: list, live_count: int, total_count: int) -> str:
    """Self-contained swipeable article deck -- open reports/index.html (or
    the Pages URL) instead of poking at news.db to see what the feeds have.
    Data is embedded as JSON; the deck, filtering and the "up next" queue
    are all built client-side from it."""
    now = datetime.now(timezone.utc)

    def sort_key(a):
        return a["published"] or datetime.min.replace(tzinfo=timezone.utc)

    payload = [
        {
            "id": hashlib.sha1(a["link"].encode()).hexdigest()[:10],
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
    data_json = json.dumps(payload).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>newsdigest</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #f2f3f7; --surface: #ffffff; --ink: #14141a; --sub: #6b7280;
    --border: rgba(20,20,30,.07); --shadow: 0 1px 2px rgba(20,20,30,.04), 0 8px 20px -12px rgba(20,20,30,.12);
    --shadow-lg: 0 10px 20px -8px rgba(20,20,30,.18), 0 30px 60px -30px rgba(20,20,30,.25);
    --accent: #6d5ef0; --header-bg: rgba(242,243,247,.75);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #0b0c0f; --surface: #1a1b1f; --ink: #f0f0f2; --sub: #9199a3;
             --border: rgba(255,255,255,.08); --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 20px -12px rgba(0,0,0,.5);
             --shadow-lg: 0 10px 20px -8px rgba(0,0,0,.5), 0 30px 60px -30px rgba(0,0,0,.7);
             --accent: #8b7bff; --header-bg: rgba(11,12,15,.75); }}
  }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  body {{
    font-family: -apple-system, "SF Pro Text", system-ui, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--ink);
    margin: 0; min-height: 100dvh; line-height: 1.45;
  }}
  header {{
    position: sticky; top: 0; z-index: 20;
    backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    background: var(--header-bg); border-bottom: 1px solid var(--border);
    padding: 1rem 1.1rem 0.8rem;
  }}
  .titlebar {{ display: flex; align-items: baseline; justify-content: space-between; max-width: 1040px; margin: 0 auto; }}
  header h1 {{
    margin: 0; font-size: 1.3rem; font-weight: 800; letter-spacing: -0.03em;
    background: linear-gradient(135deg, var(--accent), #ff6fb0);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }}
  header .stats {{ color: var(--sub); font-size: 0.76rem; font-weight: 500; }}
  .freshness {{
    display: flex; align-items: center; justify-content: space-between;
    max-width: 1040px; margin: 0.5rem auto 0; font-size: 0.76rem; color: var(--sub);
  }}
  .refresh-btn {{
    border: 1px solid var(--border); background: var(--surface); color: var(--sub);
    padding: 0.25rem 0.65rem; border-radius: 999px; font-size: 0.74rem; font-weight: 600;
    cursor: pointer; transition: transform .12s ease;
  }}
  .refresh-btn:active {{ transform: scale(0.94); }}
  .filters {{
    display: flex; gap: 0.4rem; overflow-x: auto; margin: 0.75rem auto 0;
    max-width: 1040px; padding-bottom: 0.15rem; scrollbar-width: none;
  }}
  .filters::-webkit-scrollbar {{ display: none; }}
  .filters-sub {{ margin-top: 0.45rem; }}
  .filter {{
    flex: none; border: 1px solid var(--border); background: var(--surface);
    color: var(--sub); padding: 0.35rem 0.75rem; border-radius: 999px;
    font-size: 0.78rem; font-weight: 600; cursor: pointer; white-space: nowrap;
    transition: transform .15s ease, background .15s ease, color .15s ease;
  }}
  .filter .count {{ opacity: 0.6; font-weight: 500; }}
  .filter:active {{ transform: scale(0.95); }}
  .filter.active {{ background: hsl(var(--hue, 250) 70% 50%); color: #fff; border-color: transparent; }}
  .filter[data-filter="all"].active {{ background: var(--accent); }}
  .filter[data-filter="__saved__"].active {{ background: #f5a623; }}
  .toast {{
    position: fixed; bottom: 1.3rem; left: 50%; transform: translateX(-50%) translateY(20px);
    background: var(--ink); color: var(--bg); padding: 0.6rem 1.1rem; border-radius: 999px;
    font-size: 0.85rem; opacity: 0; pointer-events: none; z-index: 50;
    transition: opacity .2s ease, transform .2s ease;
  }}
  .toast.show {{ opacity: 1; transform: translateX(-50%) translateY(0); }}

  .layout {{
    max-width: 1040px; margin: 0 auto; padding: 1.5rem 1rem 2.5rem;
    display: grid; grid-template-columns: 1fr; gap: 1.5rem; align-items: start;
  }}
  .stage {{
    position: relative; height: min(64vh, 560px);
    display: flex; align-items: center; justify-content: center;
  }}
  .swipe-card {{
    position: absolute; inset: 0; margin: auto;
    width: min(92vw, 400px); height: 100%;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 24px; box-shadow: var(--shadow-lg); overflow: hidden;
    display: flex; flex-direction: column;
    cursor: grab; user-select: none; touch-action: none;
    transition: transform .32s cubic-bezier(.2,.8,.2,1), opacity .32s ease;
  }}
  .swipe-card.dragging {{ transition: none; cursor: grabbing; }}
  .swipe-card:active {{ cursor: grabbing; }}
  .card-image {{ flex: none; height: 38%; background: var(--bg); }}
  .card-image img {{ width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none; }}
  .card-body {{
    flex: 1; min-height: 0; display: flex; flex-direction: column;
    padding: 1.4rem 1.4rem 1.2rem; overflow: hidden;
  }}
  .swipe-card .tag {{
    display: inline-flex; align-self: flex-start; background: hsl(var(--hue) 75% 92%); color: hsl(var(--hue) 60% 28%);
    padding: 0.3rem 0.7rem; border-radius: 999px; font-size: 0.74rem; font-weight: 700;
    margin-right: 0.35rem;
  }}
  @media (prefers-color-scheme: dark) {{
    .swipe-card .tag {{ background: hsl(var(--hue) 40% 20%); color: hsl(var(--hue) 75% 82%); }}
  }}
  .swipe-card .topic-tag {{ background: var(--bg); color: var(--sub); border: 1px solid var(--border); }}
  @media (prefers-color-scheme: dark) {{ .swipe-card .topic-tag {{ background: var(--bg); color: var(--sub); }} }}
  .swipe-card time {{ display: block; color: var(--sub); font-size: 0.78rem; margin: 0.6rem 0 0.9rem; }}
  .swipe-card h2 {{ font-size: 1.25rem; line-height: 1.3; font-weight: 800; letter-spacing: -0.02em; margin: 0 0 0.4rem; }}
  .also-from {{ font-size: 0.74rem; color: var(--sub); margin: 0 0 0.7rem; }}
  .swipe-card .snippet {{
    margin: 0; color: var(--sub); font-size: 0.92rem; line-height: 1.5; flex: 1;
    overflow: hidden; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical;
  }}
  .read-link {{
    align-self: flex-start; margin-top: 0.9rem; font-size: 0.86rem; font-weight: 700;
    color: var(--accent); text-decoration: none; touch-action: auto;
  }}
  .card-actions {{ position: absolute; top: 0.7rem; right: 0.7rem; z-index: 5; display: flex; gap: 0.4rem; }}
  .icon-btn {{
    width: 2.15rem; height: 2.15rem; border-radius: 50%; border: none; cursor: pointer;
    background: rgba(255,255,255,.85); color: #1a1a1a; font-size: 1.05rem;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 1px 4px rgba(0,0,0,.18); touch-action: auto;
  }}
  @media (prefers-color-scheme: dark) {{ .icon-btn {{ background: rgba(40,41,46,.85); color: #f0f0f2; }} }}
  .icon-btn.saved {{ color: #f5a623; }}
  .icon-btn:active {{ transform: scale(0.9); }}
  .read-link:hover {{ text-decoration: underline; }}
  .end-card {{ align-items: center; justify-content: center; text-align: center; cursor: default; padding: 1.4rem; }}
  .end-card .big {{ font-size: 2.6rem; margin-bottom: 0.4rem; }}
  .end-card h2 {{ margin-bottom: 0.3rem; }}

  .controls {{ display: flex; justify-content: center; align-items: center; gap: 1.4rem; margin-top: 1.1rem; }}
  .ctrl-btn {{
    width: 3.4rem; height: 3.4rem; border-radius: 50%; border: 1px solid var(--border);
    background: var(--surface); box-shadow: var(--shadow); font-size: 1.5rem;
    display: flex; align-items: center; justify-content: center; cursor: pointer; color: var(--ink);
    transition: transform .12s ease;
  }}
  .ctrl-btn:active {{ transform: scale(0.92); }}
  .counter {{ text-align: center; color: var(--sub); font-size: 0.8rem; margin-top: 0.6rem; }}
  .kbd-hint {{ display: none; text-align: center; color: var(--sub); font-size: 0.76rem; margin-top: 0.4rem; opacity: 0.7; }}

  .queue {{ display: none; }}
  .queue h3 {{ font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--sub); margin: 0 0 0.7rem; }}
  .queue-item {{
    display: flex; gap: 0.6rem; align-items: flex-start; padding: 0.55rem 0.6rem; border-radius: 12px;
    cursor: pointer; transition: background .12s ease;
  }}
  .queue-item:hover {{ background: var(--surface); }}
  .queue-item .dot {{ flex: none; width: 0.5rem; height: 0.5rem; border-radius: 50%; margin-top: 0.45rem; background: hsl(var(--hue) 70% 50%); }}
  .queue-item .qt {{ font-size: 0.85rem; font-weight: 600; line-height: 1.3; }}
  .queue-item .qs {{ font-size: 0.72rem; color: var(--sub); margin-top: 0.15rem; }}

  @media (min-width: 880px) {{
    .layout {{ grid-template-columns: minmax(0, 1fr) 300px; gap: 2rem; }}
    .stage {{ height: min(68vh, 600px); }}
    .swipe-card {{ width: min(70%, 440px); }}
    .queue {{ display: block; }}
    .kbd-hint {{ display: block; }}
  }}
  @media (prefers-reduced-motion: reduce) {{ .swipe-card {{ transition: none; }} }}
</style>
</head>
<body data-generated="{now.isoformat()}">
  <header>
    <div class="titlebar">
      <h1>newsdigest</h1>
      <span class="stats" id="stats">{live_count}/{total_count} feeds &middot; {len(articles)} articles</span>
    </div>
    <div class="freshness">
      <span id="freshness-text"></span>
      <button class="refresh-btn" id="btn-refresh" title="Check for the latest snapshot">&#8635; Refresh</button>
    </div>
    <nav class="filters" id="topic-filters"></nav>
    <nav class="filters filters-sub" id="filters"></nav>
  </header>
  <div class="layout">
    <div>
      <main class="stage" id="stage"></main>
      <div class="controls">
        <button class="ctrl-btn" id="btn-prev" title="Previous">&#8249;</button>
        <button class="ctrl-btn" id="btn-next" title="Next">&#8250;</button>
      </div>
      <div class="counter" id="counter"></div>
      <div class="kbd-hint">&larr; / &rarr; to move between articles &middot; &#9734; saves it &middot; &#8646; shares a link back to this card</div>
    </div>
    <aside class="queue" id="queue"><h3>Up next</h3><div id="queue-list"></div></aside>
  </div>
  <script id="data" type="application/json">{data_json}</script>
  <script>
  (function () {{
    const all = JSON.parse(document.getElementById('data').textContent);
    const stage = document.getElementById('stage');
    const queueList = document.getElementById('queue-list');
    const counterEl = document.getElementById('counter');
    const statsEl = document.getElementById('stats');
    const filtersEl = document.getElementById('filters');
    const topicFiltersEl = document.getElementById('topic-filters');

    const sources = [...new Set(all.map(a => a.source))].sort();
    const topics = [...new Set(all.map(a => a.topic))].sort();
    const SAVED_KEY = 'newsdigest:saved';
    let saved = new Set(JSON.parse(localStorage.getItem(SAVED_KEY) || '[]'));
    let sourceFilter = 'all';
    let topicFilter = 'all';
    let index = 0;

    function jumpToHash() {{
      const pos = all.findIndex(a => a.id === location.hash.slice(1));
      if (pos >= 0) {{
        sourceFilter = 'all'; topicFilter = 'all'; index = pos;
        renderSourceFilters(); renderTopicFilters(); render();
      }}
    }}
    if (location.hash) jumpToHash();
    // covers a shared link opened in a tab that already had the page loaded
    window.addEventListener('hashchange', jumpToHash);

    // "Refresh" doesn't trigger a new Action run -- there's no server to hold
    // the token that would take, and a token embedded in a public page is a
    // bad idea regardless. It re-fetches whatever the hourly cron has
    // already published, bypassing any stale browser/CDN cache, and is a
    // no-op (with a toast) if the page is already newer than the cooldown.
    const REFRESH_COOLDOWN_MIN = 30;
    const generatedAt = new Date(document.body.dataset.generated);

    function minutesSinceGenerated() {{
      return (Date.now() - generatedAt.getTime()) / 60000;
    }}

    function updateFreshnessText() {{
      const mins = Math.floor(minutesSinceGenerated());
      const label = mins < 1 ? 'just now' : mins < 60 ? `${{mins}}m ago` : `${{Math.floor(mins / 60)}}h ago`;
      document.getElementById('freshness-text').textContent = `Updated ${{label}}`;
    }}
    updateFreshnessText();

    document.getElementById('btn-refresh').addEventListener('click', () => {{
      const mins = minutesSinceGenerated();
      if (mins < REFRESH_COOLDOWN_MIN) {{
        toast(`Already fresh — updated ${{Math.floor(mins)}}m ago`);
        return;
      }}
      toast('Checking for the latest snapshot…');
      setTimeout(() => {{
        location.href = `${{location.pathname}}?t=${{Date.now()}}${{location.hash}}`;
      }}, 400);
    }});

    function persistSaved() {{ localStorage.setItem(SAVED_KEY, JSON.stringify([...saved])); }}

    function toast(msg) {{
      let t = document.getElementById('toast');
      if (!t) {{
        t = document.createElement('div');
        t.id = 'toast';
        t.className = 'toast';
        document.body.appendChild(t);
      }}
      t.textContent = msg;
      t.classList.add('show');
      clearTimeout(toast._timer);
      toast._timer = setTimeout(() => t.classList.remove('show'), 1800);
    }}

    function shareArticle(a) {{
      const url = `${{location.origin}}${{location.pathname}}#${{a.id}}`;
      const text = `${{a.title}} — via newsdigest`;
      if (navigator.share) {{
        navigator.share({{ title: a.title, text, url }}).catch(() => {{}});
      }} else if (navigator.clipboard) {{
        navigator.clipboard.writeText(`${{text}}\n${{url}}`).then(() => toast('Link copied'));
      }}
    }}

    function toggleSave(id) {{
      if (saved.has(id)) saved.delete(id); else saved.add(id);
      persistSaved();
      if (sourceFilter === '__saved__' && index >= filtered().length) index = 0;
      renderSourceFilters();
      render();
    }}

    function esc(s) {{
      return String(s ?? '').replace(/[&<>"']/g, c => (
        {{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }}[c]
      ));
    }}
    function safeUrl(u) {{
      return /^https?:\\/\\//i.test(u || '') ? u : '';
    }}

    function filtered() {{
      let list = sourceFilter === '__saved__' ? all.filter(a => saved.has(a.id))
        : sourceFilter === 'all' ? all : all.filter(a => a.source === sourceFilter);
      if (topicFilter !== 'all') list = list.filter(a => a.topic === topicFilter);
      return list;
    }}

    function relativeTime(iso) {{
      if (!iso) return 'undated';
      const mins = (Date.now() - new Date(iso).getTime()) / 60000;
      if (mins < 1) return 'just now';
      if (mins < 60) return Math.floor(mins) + 'm ago';
      const hours = mins / 60;
      if (hours < 24) return Math.floor(hours) + 'h ago';
      const days = hours / 24;
      if (days < 7) return Math.floor(days) + 'd ago';
      return new Date(iso).toLocaleDateString(undefined, {{ month: 'short', day: 'numeric' }});
    }}

    function renderSourceFilters() {{
      const chip = (label, value, active, hue) =>
        `<button class="filter${{active ? ' active' : ''}}" data-filter="${{esc(value)}}"` +
        (hue !== undefined ? ` style="--hue:${{hue}}"` : '') + `>${{label}}</button>`;
      let html = chip('All', 'all', sourceFilter === 'all');
      html += chip(`&#9733; Saved <span class="count">${{saved.size}}</span>`, '__saved__', sourceFilter === '__saved__');
      for (const s of sources) {{
        const count = all.filter(a => a.source === s).length;
        const hue = all.find(a => a.source === s).hue;
        html += chip(`${{esc(s)}} <span class="count">${{count}}</span>`, s, sourceFilter === s, hue);
      }}
      filtersEl.innerHTML = html;
    }}

    function renderTopicFilters() {{
      const chip = (label, value, active) =>
        `<button class="filter topic-filter${{active ? ' active' : ''}}" data-topic="${{esc(value)}}">${{label}}</button>`;
      let html = chip('All topics', 'all', topicFilter === 'all');
      for (const t of topics) {{
        const count = all.filter(a => a.topic === t).length;
        html += chip(`${{esc(t)}} <span class="count">${{count}}</span>`, t, topicFilter === t);
      }}
      topicFiltersEl.innerHTML = html;
    }}

    function renderQueue() {{
      const rest = filtered().slice(index + 1, index + 6);
      queueList.innerHTML = rest.length ? rest.map(a => `
        <div class="queue-item" style="--hue:${{a.hue}}">
          <span class="dot"></span>
          <div><div class="qt">${{esc(a.title)}}</div><div class="qs">${{esc(a.source)}}</div></div>
        </div>`).join('') : '<p style="color:var(--sub);font-size:.82rem">Nothing queued.</p>';
    }}

    function cardHtml(a) {{
      const img = a.image ? `<div class="card-image"><img loading="lazy" src="${{esc(safeUrl(a.image))}}" alt=""
        onerror="this.closest('.card-image').style.display='none'"></div>` : '';
      const also = (a.alsoFrom && a.alsoFrom.length)
        ? `<div class="also-from">+ ${{a.alsoFrom.map(esc).join(', ')}}</div>` : '';
      const isSaved = saved.has(a.id);
      return `
        <div class="card-actions">
          <button class="icon-btn save-btn${{isSaved ? ' saved' : ''}}" data-id="${{a.id}}" title="Save for later">${{isSaved ? '&#9733;' : '&#9734;'}}</button>
          <button class="icon-btn share-btn" data-id="${{a.id}}" title="Share">&#8646;</button>
        </div>
        ${{img}}
        <div class="card-body">
          <span class="tag">${{esc(a.source)}}</span>
          <span class="tag topic-tag">${{esc(a.topic)}}</span>
          <time>${{relativeTime(a.published)}}</time>
          <h2>${{esc(a.title)}}</h2>
          ${{also}}
          <p class="snippet">${{esc(a.snippet)}}</p>
          <a class="read-link" href="${{esc(safeUrl(a.link))}}" target="_blank" rel="noopener noreferrer">Read full article &#8599;</a>
        </div>`;
    }}

    function render() {{
      const list = filtered();
      stage.innerHTML = '';
      statsEl.textContent = `${{list.length}} article${{list.length === 1 ? '' : 's'}}`;

      if (index >= list.length) {{
        counterEl.textContent = list.length ? 'All caught up' : 'No articles for this filter';
        const end = document.createElement('div');
        end.className = 'swipe-card end-card';
        end.innerHTML = `<div class="big">&#127881;</div><h2>You're all caught up</h2>
          <p class="snippet" style="flex:none">Nothing left in this queue.</p>`;
        stage.appendChild(end);
        renderQueue();
        return;
      }}

      counterEl.textContent = `${{index + 1}} of ${{list.length}}`;
      const depth = Math.min(3, list.length - index);
      for (let i = depth - 1; i >= 0; i--) {{
        const a = list[index + i];
        const el = document.createElement('article');
        el.className = 'swipe-card' + (a.image ? ' has-image' : '');
        el.style.setProperty('--hue', a.hue);
        el.style.zIndex = 10 - i;
        el.style.transform = `translateY(${{i * 10}}px) scale(${{1 - i * 0.045}})`;
        el.style.opacity = i === 2 ? '0.6' : '1';
        el.innerHTML = cardHtml(a);
        if (i === 0) attachDrag(el);
        stage.appendChild(el);
      }}
      renderQueue();
    }}

    // Swiping either direction just moves to the next article -- there's no
    // like/dislike distinction. Opening an article only happens via the
    // explicit "Read full article" link, so it never gets triggered by accident.
    function advance() {{ index += 1; render(); }}
    function goBack() {{ if (index > 0) {{ index -= 1; render(); }} }}

    function attachDrag(card) {{
      let startX = 0, startY = 0, dx = 0, dy = 0;

      function onMove(e) {{
        dx = e.clientX - startX;
        dy = e.clientY - startY;
        card.style.transform = `translate(${{dx}}px, ${{dy}}px) rotate(${{dx / 18}}deg)`;
      }}

      function onUp() {{
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        card.classList.remove('dragging');
        const THRESHOLD = 110;
        if (Math.abs(dx) > THRESHOLD) {{
          const dir = dx > 0 ? 1 : -1;
          card.style.transform = `translate(${{dir * 700}}px, ${{dy}}px) rotate(${{dir * 30}}deg)`;
          card.style.opacity = '0';
          setTimeout(advance, 260);
        }} else {{
          card.style.transform = '';
        }}
      }}

      card.addEventListener('pointerdown', (e) => {{
        if (e.target.closest('.read-link, .card-actions')) return;   // let links/buttons act normally
        dx = 0; dy = 0;
        startX = e.clientX; startY = e.clientY;
        card.classList.add('dragging');
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp);
      }});
    }}

    filtersEl.addEventListener('click', (e) => {{
      const btn = e.target.closest('.filter');
      if (!btn) return;
      sourceFilter = btn.dataset.filter;
      index = 0;
      renderSourceFilters();
      render();
    }});

    topicFiltersEl.addEventListener('click', (e) => {{
      const btn = e.target.closest('.filter');
      if (!btn) return;
      topicFilter = btn.dataset.topic;
      index = 0;
      renderTopicFilters();
      render();
    }});

    document.getElementById('btn-prev').addEventListener('click', goBack);
    document.getElementById('btn-next').addEventListener('click', advance);

    stage.addEventListener('click', (e) => {{
      const saveBtn = e.target.closest('.save-btn');
      const shareBtn = e.target.closest('.share-btn');
      if (saveBtn) toggleSave(saveBtn.dataset.id);
      else if (shareBtn) {{
        const a = all.find(x => x.id === shareBtn.dataset.id);
        if (a) shareArticle(a);
      }}
    }});

    document.addEventListener('keydown', (e) => {{
      if (e.key === 'ArrowRight') advance();
      else if (e.key === 'ArrowLeft') goBack();
    }});

    renderSourceFilters();
    renderTopicFilters();
    render();
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

    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(out))
    REPORT_JSON.write_text(json.dumps(
        {"checked_at": datetime.now(timezone.utc).isoformat(), "feeds": rows},
        indent=2,
    ))
    deduped = dedupe_articles(all_articles)
    REPORT_HTML.write_text(render_html(deduped, len(live), len(rows)))

    merged = len(all_articles) - len(deduped)
    print(f"\nwrote {REPORT_MD.name} + {REPORT_JSON.name} + {REPORT_HTML.name} "
          f"({len(live)}/{len(rows)} usable, {len(deduped)} articles, "
          f"{merged} cross-agency duplicate{'s' if merged != 1 else ''} merged)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
