"""
Feed health check.

Pings every feed in feeds.yaml and writes:
  docs/feed_check.md    -- human-readable, for reading on a phone
  docs/feed_check.json  -- machine-readable, consumed by the ingest stage

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
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.yaml"
BIAS_FILE = ROOT / "source_bias.yaml"
NOT_RATED = {"leaning": "Not rated", "cite_name": None, "cite_url": None}
REPORT_MD = ROOT / "docs" / "feed_check.md"
REPORT_JSON = ROOT / "docs" / "feed_check.json"
REPORT_HTML = ROOT / "docs" / "index.html"

# Polite, identifiable UA tried first.
UA_BOT = "NewsFlick/0.1 (personal research; +https://github.com/devshrawin/newsdigest)"
# Several Indian publishers 403 anything that isn't a browser. Retried with this.
UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

TIMEOUT = 12   # 32 feeds x 2 attempts must fit well inside the 45-min loop interval
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
            # Buffer at most MAX_BYTES, then drop the connection. Flagged on
            # the response (not just silently truncated) so check() can
            # surface it in the row's note -- a feed cut off mid-XML parses
            # with fewer entries or a bozo warning and nothing said why.
            chunks, total = [], 0
            for chunk in resp.iter_content(65536):
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BYTES:
                    break
            resp._newsdigest_body = b"".join(chunks)
            resp._newsdigest_truncated = total > MAX_BYTES
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


# Section name in the article's own URL path (indianexpress.com/section/
# sports/..., timesofindia.../business/...) -- checked and confirmed this
# is a stronger, cleaner signal than entry.tags in practice: most feeds
# here don't emit <category> at all, and The Hindu's does but emits place
# names ("Karnataka"), not topics, so it never matches TOPIC_KEYWORDS.
# Publishers are far more consistent about putting the section in the URL.
URL_TOPIC_HINTS = {
    "sport": "Sports", "sports": "Sports", "cricket": "Sports",
    "business": "Business", "markets": "Business", "money": "Business",
    "entertainment": "Entertainment", "bollywood": "Entertainment",
    "movies": "Entertainment", "movie-reviews": "Entertainment",
    "technology": "Technology", "tech": "Technology", "gadgets": "Technology",
    "politics": "Politics",
    "world": "World", "international": "World",
    "health": "Health",
}


def topic_hint_from_url(link: str) -> str | None:
    if not link:
        return None
    segments = re.split(r"[/_-]", urlparse(link).path.lower())
    for seg in segments:
        if seg in URL_TOPIC_HINTS:
            return URL_TOPIC_HINTS[seg]
    return None


def classify_topic(title: str, snippet: str, tags: list | None = None,
                    url_hint: str | None = None) -> str:
    # The URL path is checked first and, if it matches, wins outright --
    # a publisher's own section taxonomy in its own URL is about as
    # authoritative as this gets, more so than keyword-matching a headline
    # plus a ~150-char teaser (most feeds here are teaser-only, which
    # simply doesn't contain enough words to hit the keyword lists
    # reliably). entry.tags is a weaker fallback signal folded into the
    # same keyword scoring below, not a separate pass -- a publisher's tag
    # vocabulary ("Cricket", "Bollywood") doesn't match our bucket names
    # directly, so it still has to go through pattern matching, just
    # weighted toward winning via repetition.
    if url_hint:
        return url_hint
    tag_text = " ".join(tags or [])
    text = f"{tag_text} {tag_text} {tag_text} {title} {snippet}"
    best_topic, best_hits = "General", 0
    for topic, patterns in TOPIC_PATTERNS.items():
        hits = sum(1 for p in patterns if p.search(text))
        if hits > best_hits:
            best_topic, best_hits = topic, hits
    return best_topic


# Geography, not subject -- orthogonal to TOPIC_KEYWORDS. A Business story
# can be about China's economy (Asia) or the US Fed (Americas) just as
# easily as India's budget (India), so this needs its own keyword pass
# rather than reusing the topic classifier's "World" bucket, which only
# tags foreign-affairs-flavored stories, not every article's setting.
REGION_KEYWORDS = {
    "India": [
        "india", "indian", "delhi", "mumbai", "bengaluru", "bangalore",
        "chennai", "kolkata", "hyderabad", "pune", "modi", "bjp",
        "lok sabha", "rajya sabha", "rupee",
    ],
    "Asia": [
        "china", "chinese", "japan", "japanese", "korea", "korean",
        "pakistan", "bangladesh", "sri lanka", "nepal", "myanmar",
        "singapore", "malaysia", "indonesia", "thailand", "vietnam",
        "philippines", "taiwan", "hong kong",
    ],
    "Middle East": [
        "israel", "palestine", "gaza", "iran", "iraq", "saudi", "emirates",
        "dubai", "qatar", "syria", "lebanon", "yemen", "middle east",
    ],
    "Europe": [
        "britain", "london", "france", "paris", "germany", "berlin",
        "italy", "rome", "spain", "madrid", "russia", "moscow", "ukraine",
        "european union", "brussels",
    ],
    "Africa": [
        "nigeria", "kenya", "south africa", "egypt", "ethiopia", "ghana",
        "african",
    ],
    "Americas": [
        "united states", "washington", "biden", "trump", "canada",
        "mexico", "brazil", "argentina", "american",
    ],
    "Oceania": [
        "australia", "australian", "sydney", "melbourne", "new zealand",
        "auckland",
    ],
}
REGION_PATTERNS = {
    region: [re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE) for kw in kws]
    for region, kws in REGION_KEYWORDS.items()
}


def classify_region(title: str, snippet: str, default_region: str | None = None) -> str:
    text = f"{title} {snippet}"
    best_region, best_hits = "Other", 0
    for region, patterns in REGION_PATTERNS.items():
        hits = sum(1 for p in patterns if p.search(text))
        if hits > best_hits:
            best_region, best_hits = region, hits
    # No keyword hit at all, not "hit something ambiguous" -- a domestic
    # feed's story about a specific foreign country still keyword-matches
    # to that region above; this only covers the story that mentions
    # neither, which an India-news feed's default_region="India" should
    # resolve to India, not the meaningless "Other" bucket.
    if best_hits == 0 and default_region:
        return default_region
    return best_region


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


def check(name: str, url: str, default_region: str | None = None):
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
    truncated_note = f"truncated at {MAX_BYTES // (1024 * 1024)}MB" if getattr(resp, "_newsdigest_truncated", False) else ""
    row["entries"] = len(entries)
    if not entries:
        row["note"] = "0 entries (not a feed? moved?)" + (f", {truncated_note}" if truncated_note else "")
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
    notes = [n for n in (note, bozo_note, truncated_note) if n]
    if resp.url.rstrip("/") != url.rstrip("/"):
        notes.append(f"redirected -> {resp.url}")
    row["note"] = "; ".join(notes)

    for e, body, t in zip(entries, bodies, entry_times):
        title = e.get("title") or "(untitled)"
        snippet = (body[:SNIPPET_LEN] + "…") if len(body) > SNIPPET_LEN else body
        tags = [tag.get("term") for tag in (e.get("tags") or []) if tag.get("term")]
        link = entry_link(e)
        articles.append({
            "source": name,
            "title": title,
            "link": link,
            "published": t,
            "snippet": snippet,
            "image": entry_image(e),
            "topic": classify_topic(title, body, tags, topic_hint_from_url(link)),
            "region": classify_region(title, body, default_region),
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


def load_bias() -> dict:
    """source name -> {leaning, cite_name, cite_url}. Never raises: this file
    is meant to be incomplete and hand-edited, so a missing file or an
    unlisted source both just fall back to Not rated rather than breaking
    the run -- unlike load_feeds(), where a bad file is a real error."""
    if not BIAS_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(BIAS_FILE.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    sources = (data or {}).get("sources")
    if not isinstance(sources, dict):
        return {}
    out = {}
    for name, entry in sources.items():
        if isinstance(entry, dict) and entry.get("leaning"):
            out[name] = {
                "leaning": entry["leaning"],
                "cite_name": entry.get("cite_name"),
                "cite_url": entry.get("cite_url"),
            }
    return out


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


def _dedupe_articles_naive(articles: list) -> list:
    """Reference implementation -- O(n^2), every article scanned against
    every existing cluster in creation order, first match wins. Kept only
    as the ground truth tests/test_pipeline.py diffs the real
    dedupe_articles() against; never called from the pipeline itself.
    Do not "clean this up" -- its whole value is being an unoptimized,
    obviously-correct copy of the original algorithm."""
    clusters = []

    def better(a, b):
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


def _title_tokens(norm: str) -> set:
    """Words over 3 chars -- long enough to be load-bearing (a proper noun,
    a keyword) rather than a stopword ("the", "and") that would appear in
    nearly every headline and defeat the index below."""
    return {w for w in norm.split() if len(w) > 3}


def dedupe_articles(articles: list) -> list:
    """Collapse near-duplicate headlines (typically the same wire story --
    PTI/ANI/Reuters -- run by multiple publishers) into one card, keeping the
    best-looking representative and listing who else carried it.

    Same algorithm as _dedupe_articles_naive above -- same match order, same
    `better()`, same anchor pinning -- just narrowed to a smaller candidate
    set per article before any SequenceMatcher call, since scanning every
    prior cluster against every article is quadratic and measurably the
    pipeline's dominant cost past a few hundred articles (~14 min projected
    at the real ~3,000-article scale, most of a 45-min loop budget).

    Two narrowings, both exact (neither can accept a pair the naive version
    would have rejected, or reject one it would have accepted):
      1. A token index maps each cluster's significant words (>3 chars) to
         the cluster's index, so an article only gets compared against
         clusters it shares at least one such word with -- two headlines
         about the same story necessarily share a proper noun or keyword
         at that length. Titles with no word that long (rare) fall back to
         scanning every cluster, same as the naive version always does.
      2. SequenceMatcher.real_quick_ratio()/quick_ratio() are cheap exact
         upper bounds on ratio() -- checked first, so a pair that can't
         possibly reach the threshold never pays for the real comparison.
    Candidates are still visited in creation order (ascending cluster
    index) so first-match-wins semantics exactly match the naive version.
    tests/test_pipeline.py::test_dedupe_matches_naive_reference asserts
    the two produce identical output on every change here -- don't touch
    either function without running it.
    """
    clusters = []   # each: {"anchor": str, "at": datetime|None, "rep": article, "members": [source]}
    token_index = defaultdict(list)   # token -> [cluster index, ...], append-order == creation order

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
        toks = _title_tokens(norm)
        if toks:
            candidate_idxs = sorted(set().union(*(token_index[t] for t in toks)))
        else:
            candidate_idxs = range(len(clusters))

        match = None
        for idx in candidate_idxs:
            c = clusters[idx]
            if a["published"] and c["at"]:
                gap_hours = abs((a["published"] - c["at"]).total_seconds()) / 3600
                if gap_hours > DEDUPE_WINDOW_HOURS:
                    continue
            sm = difflib.SequenceMatcher(None, norm, c["anchor"])
            if sm.real_quick_ratio() < DEDUPE_TITLE_THRESHOLD:
                continue
            if sm.quick_ratio() < DEDUPE_TITLE_THRESHOLD:
                continue
            if sm.ratio() >= DEDUPE_TITLE_THRESHOLD:
                match = c
                break
        if match is None:
            clusters.append({"anchor": norm, "at": a["published"], "rep": a, "members": [a["source"]]})
            new_idx = len(clusters) - 1
            for t in toks:
                token_index[t].append(new_idx)
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


def round_robin_by_source(articles: list, limit: int) -> list:
    """Cap the deck at `limit` without letting whichever publisher posts
    most often crowd out everyone else. A straight "sort by time, take the
    newest N" let two cricket feeds alone eat a quarter of a 400-card deck
    on a normal day -- posting frequency, not relevance, decided who made
    the cut. This drains one article per source per round instead, so a
    low-volume feed's newest item competes on recency against a
    high-volume feed's newest item, not against that feed's whole backlog.

    `articles` must already be sorted newest-first; each source's queue
    then drains oldest-of-its-newest first, which is what makes the
    interleave fair rather than favoring whichever source happens to sort
    first within a round.
    """
    by_source = defaultdict(deque)
    for a in articles:
        by_source[a["source"]].append(a)

    deck = []
    queues = list(by_source.values())
    while len(deck) < limit and queues:
        for q in queues:
            if not q:
                continue
            if len(deck) >= limit:
                break
            deck.append(q.popleft())
        queues = [q for q in queues if q]

    deck.sort(
        key=lambda a: a["published"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return deck


def render_html(articles: list) -> str:
    """Self-contained swipeable article deck -- open docs/index.html (or
    the Pages URL) instead of poking at news.db to see what the feeds have.

    Everything (layout, filtering, the deck, the up-next queue) is built
    client-side from an embedded JSON payload, so all interpolated text has
    to go through the page's esc() and every URL through safeUrl(). See the
    audit note in the README before touching that.
    """
    now = datetime.now(timezone.utc)
    bias = load_bias()

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
            "region": a.get("region", "Other"),
            "published": a["published"].isoformat() if a["published"] else None,
            "initials": source_initials(a["source"]),
            "alsoFrom": a.get("also_from", []),
            "leaning": bias.get(a["source"], NOT_RATED)["leaning"],
            "citeName": bias.get(a["source"], NOT_RATED)["cite_name"],
            "citeUrl": bias.get(a["source"], NOT_RATED)["cite_url"],
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
<title>NewsFlick</title>
<link rel="manifest" href="manifest.json">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#141310">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f4f1e8">
<link rel="icon" type="image/png" sizes="32x32" href="favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="favicon-16.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="NewsFlick">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Spectral:ital,wght@0,400;0,600;0,800;1,400&family=Archivo:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  /* Dark is the default look regardless of system preference -- only an
     explicit `prefers-color-scheme: light` gets the light palette below.
     `color-scheme: dark` on :root also tells native form controls and
     scrollbars to render dark instead of fighting the page. */
  :root {{
    color-scheme: dark;
    --bg: #141310;
    --bg-2: #1d1b16;
    --glass: #1d1b16;
    --glass-2: #2a2721;
    --ink: #f0ece0;
    --sub: #b3ad9c;
    --line: #33302a;
    --line-2: #474338;
    --accent: #c2542f;
    --accent-2: rgba(194, 84, 47, .2);
    --gold: #c2542f;
    --shadow-sm: 0 6px 16px -10px rgba(0, 0, 0, .55);
    --shadow-md: 0 14px 32px -14px rgba(0, 0, 0, .65);
    --shadow-xl: 0 24px 60px -12px rgba(0, 0, 0, .7);
    --scrim: rgba(0, 0, 0, .6);
    --radius: 3px;
    --font-serif: "Spectral", Georgia, serif;
    --font-sans: "Archivo", system-ui, sans-serif;
    --font-mono: "JetBrains Mono", ui-monospace, monospace;
    /* Overshoot for anything that should feel physical; flat-out for the rest. */
    --spring: cubic-bezier(.34, 1.4, .64, 1);
    --out: cubic-bezier(.22, 1, .36, 1);
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      color-scheme: light;
      --bg: #f4f1e8;
      --bg-2: #eae5d6;
      --glass: #fbf9f3;
      --glass-2: #eae5d6;
      --ink: #17150f;
      --sub: #5c574a;
      --line: #d8d3c3;
      --line-2: #c3bdaa;
      --accent: #a33f22;
      --accent-2: rgba(163, 63, 34, .12);
      --gold: #a33f22;
      --shadow-sm: 0 6px 16px -12px rgba(23, 21, 15, .16);
      --shadow-md: 0 14px 32px -18px rgba(23, 21, 15, .2);
      --shadow-xl: 0 24px 60px -18px rgba(23, 21, 15, .24);
      --scrim: rgba(23, 21, 15, .45);
    }}
  }}

  /* Explicit overrides for the in-app theme switch, so a user's choice wins
     regardless of what the OS/browser reports. Higher specificity than the
     plain :root above (attribute selector beats none) and than the
     prefers-color-scheme blocks (media queries don't add specificity), so
     these apply whenever the JS below sets data-theme -- and are inert
     (matching nothing) when it doesn't, leaving the media-query behavior as
     the "Auto" default. */
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --bg: #141310; --bg-2: #1d1b16;
    --glass: #1d1b16; --glass-2: #2a2721;
    --ink: #f0ece0; --sub: #b3ad9c;
    --line: #33302a; --line-2: #474338;
    --accent: #c2542f; --accent-2: rgba(194, 84, 47, .2); --gold: #c2542f;
    --shadow-sm: 0 6px 16px -10px rgba(0, 0, 0, .55);
    --shadow-md: 0 14px 32px -14px rgba(0, 0, 0, .65);
    --shadow-xl: 0 24px 60px -12px rgba(0, 0, 0, .7);
    --scrim: rgba(0, 0, 0, .6);
  }}
  :root[data-theme="light"] {{
    color-scheme: light;
    --bg: #f4f1e8; --bg-2: #eae5d6;
    --glass: #fbf9f3; --glass-2: #eae5d6;
    --ink: #17150f; --sub: #5c574a;
    --line: #d8d3c3; --line-2: #c3bdaa;
    --accent: #a33f22; --accent-2: rgba(163, 63, 34, .12); --gold: #a33f22;
    --shadow-sm: 0 6px 16px -12px rgba(23, 21, 15, .16);
    --shadow-md: 0 14px 32px -18px rgba(23, 21, 15, .2);
    --shadow-xl: 0 24px 60px -18px rgba(23, 21, 15, .24);
    --scrim: rgba(23, 21, 15, .45);
  }}

  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  /* Not height:100% -- that forced the page to exactly one viewport tall
     regardless of content, so the much-shorter desktop landscape cards left
     a large empty strip below. Content sizes itself now. */
  body {{
    margin: 0;
    font-family: var(--font-sans);
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
    pointer-events: none; opacity: .3; filter: blur(60px);
    background:
      radial-gradient(38% 44% at 18% 22%, color-mix(in oklab, var(--accent) 26%, transparent), transparent 70%),
      radial-gradient(34% 40% at 82% 12%, color-mix(in oklab, var(--accent-2) 20%, transparent), transparent 70%),
      radial-gradient(40% 38% at 52% 46%, color-mix(in oklab, var(--gold) 14%, transparent), transparent 72%);
    animation: drift 26s var(--out) infinite alternate;
  }}
  @keyframes drift {{
    from {{ transform: translate3d(-3%, -2%, 0) scale(1); }}
    to   {{ transform: translate3d(4%, 3%, 0) scale(1.12); }}
  }}

  /* ---- slim top bar: menu toggle + brand + freshness ---- */
  header {{
    position: sticky; top: 0; z-index: 20;
    background: var(--bg);
    /* The heavier double-rule masthead border is a desktop editorial touch
       (see the >=900px override below) -- on a narrow phone screen, --ink
       is near-white in dark mode, so the full 3px double rule plus the
       progress rail right under it reads as one loud white bar. Plain and
       thin here instead. */
    border-bottom: 1px solid var(--line);
    /* viewport-fit=cover lets content draw under the notch/status bar, so
       the sticky header needs the safe-area inset added back in, not just
       the flat .7rem. */
    padding: calc(.7rem + env(safe-area-inset-top)) 1rem .7rem;
  }}
  .bar {{
    display: flex; align-items: center; gap: .7rem;
    max-width: 1100px; margin: 0 auto;
  }}
  .menu-btn {{
    flex: none; width: 2.3rem; height: 2.3rem; display: grid; place-items: center;
    border: 1px solid var(--line-2); background: var(--glass-2); color: var(--ink);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    border-radius: 0; cursor: pointer;
    transition: transform .18s var(--spring), border-color .18s, background .18s;
  }}
  .menu-btn svg {{ width: 1.05rem; height: 1.05rem; }}
  .menu-btn:hover {{ border-color: var(--accent); }}
  .menu-btn:active {{ transform: scale(.92); }}
  .menu-btn .ln {{ transform-origin: center; transition: transform .22s var(--out), opacity .18s; }}
  .menu-btn.open .ln1 {{ transform: translateY(6px) rotate(45deg); }}
  .menu-btn.open .ln2 {{ opacity: 0; }}
  .menu-btn.open .ln3 {{ transform: translateY(-6px) rotate(-45deg); }}
  .brand {{
    display: flex; align-items: center; gap: .5rem;
    font-family: var(--font-serif); font-size: 1.4rem; font-weight: 800; letter-spacing: -.02em;
  }}
  .brand-mark {{ width: 1.5rem; height: 1.5rem; flex: none; }}
  .ghost {{
    display: inline-flex; align-items: center; gap: .34rem;
    border: 1px solid var(--line-2); background: var(--glass-2); color: var(--sub);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    padding: .32rem .62rem; border-radius: 0;
    font-family: var(--font-sans); font-size: .72rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; cursor: pointer;
    transition: transform .18s var(--spring), color .18s, border-color .18s, background .18s;
  }}
  .ghost:hover {{ color: var(--ink); border-color: var(--accent); }}
  .ghost:active {{ transform: scale(.93); }}
  .ghost svg {{ width: .82rem; height: .82rem; }}
  .ghost.spin svg {{ animation: spin .7s var(--out); }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

  .rail {{ max-width: 1100px; margin: .55rem auto 0; height: 2px; background: var(--line); border-radius: 2px; }}
  .rail i {{
    display: block; height: 100%; width: 0; border-radius: 2px;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    transition: width .45s var(--out);
  }}

  /* ---- chips, shared by the drawer's two sections and the onboarding form ---- */
  .chipwrap {{ display: flex; flex-wrap: wrap; gap: .4rem; }}
  .chip {{
    border: 1px solid var(--line-2); background: none; color: var(--sub);
    padding: .38rem .6rem; border-radius: 0;
    font-family: var(--font-sans); font-size: .68rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
    white-space: nowrap; cursor: pointer;
    transition: transform .2s var(--spring), background .2s var(--out), color .2s, border-color .2s;
    animation: chipIn .4s var(--out) both;
    animation-delay: calc(var(--i, 0) * 22ms);
  }}
  @keyframes chipIn {{
    from {{ opacity: 0; transform: translateY(6px) scale(.94); }}
    to   {{ opacity: 1; transform: none; }}
  }}
  .chip:hover {{ color: var(--ink); border-color: var(--ink); }}
  .chip:active {{ transform: scale(.94); }}
  .chip.on {{
    color: var(--bg); border-color: var(--ink); background: var(--ink);
  }}
  .chip:disabled {{ opacity: .35; cursor: default; pointer-events: none; }}

  /* ---- collapsible left drawer (topics + sources moved out of the top bar) ---- */
  .scrim {{
    position: fixed; inset: 0; z-index: 29; background: var(--scrim);
    opacity: 0; pointer-events: none; transition: opacity .25s var(--out);
  }}
  .scrim.show {{ opacity: 1; pointer-events: auto; }}
  .drawer {{
    position: fixed; inset: 0 auto 0 0; z-index: 30; width: min(84vw, 320px);
    background: var(--glass); border-right: 1px solid var(--line);
    backdrop-filter: blur(38px) saturate(1.7); -webkit-backdrop-filter: blur(38px) saturate(1.7);
    box-shadow: var(--shadow-xl);
    transform: translateX(-100%); transition: transform .32s var(--out);
    display: flex; flex-direction: column; overflow: hidden;
  }}
  .drawer.open {{ transform: translateX(0); }}
  .drawer-head {{
    flex: none; display: flex; align-items: center; justify-content: space-between;
    padding: 1rem 1.1rem; border-bottom: 3px double var(--ink);
  }}
  .drawer-head strong {{ font-family: var(--font-serif); font-size: 1.4rem; font-weight: 800; letter-spacing: -.02em; }}
  .drawer-close {{
    width: 2rem; height: 2rem; display: grid; place-items: center; border-radius: 0;
    border: 1px solid transparent; background: none; color: var(--sub); cursor: pointer;
  }}
  .drawer-close:hover {{ color: var(--ink); background: var(--bg-2); }}
  .drawer-close svg {{ width: 1rem; height: 1rem; }}
  .drawer-body {{ flex: 1; overflow-y: auto; padding: .4rem 0 1.4rem; }}

  .section-head {{
    width: 100%; display: flex; align-items: center; justify-content: space-between; gap: .5rem;
    padding: .9rem 1.1rem .5rem; border: none; background: none; cursor: pointer;
    font: inherit; color: var(--ink); text-align: left;
  }}
  .section-head span.t {{
    font-family: var(--font-mono); font-size: .68rem; font-weight: 500;
    letter-spacing: .14em; text-transform: uppercase; color: var(--sub);
  }}
  .section-head svg {{
    width: .85rem; height: .85rem; color: var(--sub);
    transition: transform .25s var(--out);
  }}
  .section.collapsed .section-head svg {{ transform: rotate(-90deg); }}
  .section-panel {{
    padding: .1rem 1.1rem 1rem;
    display: grid; grid-template-rows: 1fr; transition: grid-template-rows .28s var(--out);
  }}
  .section-panel > div {{ overflow: hidden; }}
  .section.collapsed .section-panel {{ grid-template-rows: 0fr; }}
  .section.collapsed .section-panel > div {{ opacity: 0; }}
  .section-hint {{ font-size: .72rem; color: var(--sub); margin: 0 0 .55rem; }}
  .theme-switch {{
    display: flex; gap: 0; margin: 0 1.1rem 1rem; padding: 0;
    background: none; border: 1px solid var(--line-2); border-radius: 0;
  }}
  .theme-opt {{
    flex: 1; border: none; border-right: 1px solid var(--line-2); background: none; color: var(--sub); cursor: pointer;
    font-family: var(--font-sans); font-size: .68rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
    padding: .5rem 0; border-radius: 0;
    transition: background .18s var(--out), color .18s;
  }}
  .theme-opt:last-child {{ border-right: 0; }}
  .theme-opt.on {{ background: var(--ink); color: var(--bg); box-shadow: none; }}

  .drawer-footnote {{
    font-size: .7rem; color: var(--sub); opacity: .75; line-height: 1.4;
    padding: .9rem 1.1rem 0; margin: .3rem 0 0; border-top: 1px solid var(--line);
  }}
  .drawer-fresh {{
    font-family: var(--font-mono); font-size: .68rem; color: var(--sub); opacity: .6;
    margin: .7rem 0 0; padding: 0 1.1rem;
  }}
  .drawer-show {{
    display: block; width: calc(100% - 2.2rem); margin: 1rem 1.1rem 0;
    padding: .8rem 1rem; border-radius: 2px; border: 1px solid var(--ink); background: var(--ink); color: var(--bg);
    font-family: var(--font-sans); font-weight: 700; font-size: .74rem; letter-spacing: .1em; text-transform: uppercase;
    cursor: pointer;
  }}
  .drawer-show:hover {{ opacity: .85; }}
  .drawer-keys {{
    font-family: var(--font-mono); font-size: .66rem; color: var(--sub); opacity: .6; line-height: 1.5;
    margin: .7rem 0 0; padding: 0 1.1rem;
  }}

  /* ---- first-run "what do you care about" onboarding form ---- */
  .onb-scrim {{
    position: fixed; inset: 0; z-index: 15; background: var(--scrim);
    display: flex; align-items: flex-end; justify-content: center;
    opacity: 0; pointer-events: none; transition: opacity .28s var(--out);
  }}
  /* Sits below the header (z-index 20) on purpose: the onboarding prompt
     should gate the deck, not the hamburger/refresh controls. It used to sit
     above everything (z-index 50), so the very first tap on the hamburger
     landed on this backdrop instead -- geometrically correct (the backdrop
     covers the full viewport for tap-outside-to-dismiss), but it meant that
     tap silently just closed the prompt instead of opening the drawer, which
     read as "the hamburger doesn't do anything." */
  .onb-scrim.show {{ opacity: 1; pointer-events: auto; }}
  .onb {{
    position: relative;
    width: 100%; max-width: 480px; max-height: 86vh; overflow-y: auto;
    background: var(--glass); border: 1px solid var(--line);
    backdrop-filter: blur(40px) saturate(1.7); -webkit-backdrop-filter: blur(40px) saturate(1.7);
    border-radius: 26px 26px 0 0; box-shadow: var(--shadow-xl);
    padding: 1.6rem 1.4rem calc(1.4rem + env(safe-area-inset-bottom, 0px));
    transform: translateY(24px); transition: transform .32s var(--spring);
  }}
  .onb::before {{
    content: ""; display: block; width: 40px; height: 4px; border-radius: 99px;
    background: var(--line-2); margin: 0 auto 1.4rem;
  }}
  .onb-scrim.show .onb {{ transform: translateY(0); }}
  .onb h2 {{ margin: 0 0 .3rem; font-family: var(--font-serif); font-weight: 800; font-size: 1.6rem; letter-spacing: -.02em; }}
  .onb p {{ margin: 0 0 1.1rem; color: var(--sub); font-size: .86rem; }}
  .onb .chipwrap {{ margin-bottom: 1.3rem; }}
  .onb .chip {{ font-size: .82rem; padding: .42rem .8rem; }}
  .onb-actions {{ display: flex; align-items: center; justify-content: space-between; gap: 1rem; }}
  .onb-skip {{
    border: none; background: none; color: var(--sub); font: inherit; font-size: .82rem;
    font-weight: 650; cursor: pointer; padding: .5rem 0;
  }}
  .onb-skip:hover {{ color: var(--ink); text-decoration: underline; }}
  .onb-go {{
    border: none; border-radius: 0; padding: .68rem 1.35rem; cursor: pointer;
    font-family: var(--font-sans); font-size: .78rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--bg);
    background: var(--ink);
    box-shadow: none;
    transition: transform .18s var(--spring);
  }}
  .onb-go:active {{ transform: scale(.96); }}

  @media (min-width: 640px) {{
    .onb-scrim {{ align-items: center; }}
    .onb {{ border-radius: 0; }}
  }}

  /* ---- "add to home screen" nudge (iOS Safari only -- see boot JS) ---- */
  .onb.a2hs {{ position: relative; }}
  .a2hs-close {{
    position: absolute; top: 1rem; right: 1rem; width: 2rem; height: 2rem;
    border: none; background: var(--glass-2); border-radius: 0; color: var(--sub);
    display: grid; place-items: center; cursor: pointer;
  }}
  .a2hs-close svg {{ width: .85rem; height: .85rem; }}
  .a2hs-close:hover {{ color: var(--ink); }}
  .a2hs-steps {{
    margin: 0; padding-left: 1.2rem; color: var(--sub); font-size: .86rem; line-height: 1.6;
  }}
  .a2hs-steps b {{ color: var(--ink); font-weight: 600; }}

  .layout {{
    position: relative; z-index: 1;
    max-width: 1100px; margin: 0 auto;
    padding: 1.25rem 1rem calc(2.5rem + env(safe-area-inset-bottom));
    display: grid; grid-template-columns: 1fr; gap: 1.5rem; align-items: start;
  }}

  .stage {{
    position: relative;
    /* Leaves clear room below the card for .ctrls -- this used to be tall
       enough that the card's own drop shadow visually ran into the prev/next
       buttons on short viewports. */
    height: clamp(380px, 54vh, 500px);
    margin-bottom: .4rem;
    perspective: 1400px;
  }}

  .card {{
    position: absolute; inset: 0; margin: auto;
    /* min(...,100%) caps growth at the column's own width so the card can
       never overlap whatever sits beside .stage (the Up Next queue on desktop). */
    width: min(94%, 400px); height: 100%;
    display: flex; flex-direction: column; overflow: hidden;
    background: var(--glass); border: 1px solid var(--line);
    backdrop-filter: blur(26px) saturate(1.6); -webkit-backdrop-filter: blur(26px) saturate(1.6);
    border-radius: var(--radius);
    user-select: none;   /* dragging the card must not highlight its text */
    /* --shadow-md, not -xl -- the wider shadow's blur/spread bled far enough
       below the card to visually run into the prev/next buttons. */
    box-shadow: var(--shadow-md);
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

  /* Lead image now lives inside .body, after the headline/summary -- a
     fixed-height band rather than a masthead, matching the mockup's
     "text first, photo is a footnote" card. */
  /* flex:1, not a fixed height/% -- a fixed size either got squeezed by a
     long headline+summary (fighting h2/.snip for room, worsening the
     mid-line-clip bug above) or left a dead gap under a short card's
     "Read full article" when there wasn't enough text to fill it. Grows
     to whatever .body has left over, floor/ceiling keep it sane at
     either extreme. */
  .media {{
    position: relative; flex: 1 1 auto; min-height: 7rem; max-height: 60%; background: var(--bg-2);
    overflow: hidden; margin: .7rem 0 0; border: 1px solid var(--line);
  }}
  .media img {{
    width: 100%; height: 100%; object-fit: cover; display: block;
    opacity: 0; transform: scale(1.06);
    transition: opacity .55s var(--out), transform 1.1s var(--out);
    -webkit-user-drag: none; user-select: none; -webkit-touch-callout: none;
    pointer-events: none;   /* the card element owns the drag, not the <img> */
  }}
  .media img.in {{ opacity: 1; transform: none; }}
  /* Shimmer sits under the image and is simply covered once it paints. */
  .media::before {{
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(100deg, var(--bg-2) 20%, var(--glass-2) 40%, var(--bg-2) 60%);
    background-size: 220% 100%;
    animation: shimmer 1.5s linear infinite;
  }}
  .media.done::before {{ display: none; }}
  @keyframes shimmer {{ to {{ background-position: -220% 0; }} }}
  .media .scrim {{
    position: absolute; inset: auto 0 0 0; height: 55%;
    background: linear-gradient(to top, var(--bg-2), transparent);
    pointer-events: none;
  }}
  .media-cap {{
    position: absolute; left: .5rem; bottom: .5rem;
    font-family: var(--font-mono); font-size: .62rem; letter-spacing: .08em; text-transform: uppercase;
    color: var(--bg); background: rgba(0,0,0,.55); padding: .22rem .45rem;
  }}
  /* No overflow-y here on purpose: a scrollable body inside a
     pointer-drag-driven card fights the swipe gesture and, on some
     browsers, paints its own bulky native scrollbar over the card. The
     parent .card already clips anything that doesn't fit. */
  .body {{ flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 1.05rem 1.15rem 1.1rem; }}
  .metarow {{
    display: flex; align-items: center; justify-content: space-between; gap: .4rem;
    margin-bottom: .5rem; padding-bottom: .55rem; border-bottom: 1px solid var(--line);
    flex: none;
  }}
  .metarow .read {{ flex: none; }}
  .topicrow {{ display: flex; align-items: center; gap: .45rem; flex-wrap: wrap; margin-bottom: .55rem; flex: none; }}
  .region-tag {{ font-family: var(--font-mono); font-size: .64rem; letter-spacing: .1em; text-transform: uppercase; color: var(--sub); }}
  .src {{
    display: inline-flex; align-items: center; gap: .5rem;
    font-size: .82rem; font-weight: 500; letter-spacing: -.005em; color: var(--ink);
  }}
  .ava {{
    width: 1.4rem; height: 1.4rem; border-radius: 0; flex: none;
    display: grid; place-items: center;
    font-family: var(--font-serif); font-size: .68rem; font-weight: 800; letter-spacing: -.02em;
    color: var(--bg); background: var(--ink);
  }}
  .pill {{
    font-family: var(--font-mono);
    font-size: .64rem; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
    color: var(--bg); background: var(--ink);
    border: 1px solid var(--ink); padding: .2rem .5rem; border-radius: 0;
  }}
  /* Print registration marks -- purely decorative, same four dots after
     every topic pill, no meaning tied to the story itself. */
  .regmarks {{ display: inline-flex; align-items: center; gap: .22em; }}
  .regmarks i {{ width: 5px; height: 5px; border-radius: 50%; display: inline-block; }}
  .regmarks i:nth-child(1) {{ background: #17150f; }}
  .regmarks i:nth-child(2) {{ background: #ffd400; }}
  .regmarks i:nth-child(3) {{ background: #ec1c5c; }}
  .regmarks i:nth-child(4) {{ background: #00aeef; }}
  .regmarks-lg {{ gap: .5em; margin-bottom: 1.6rem; }}
  .regmarks-lg i {{ width: 11px; height: 11px; }}
  /* Political-leaning pill. Self-curated (source_bias.yaml), not from an
     API -- see the drawer footer disclaimer. A source with no entry gets no
     pill at all, so "unrated" never gets mistaken for a "Center" judgment.
     One muted dashed style for every value on purpose -- no per-direction
     color-coding (no red/blue), since that reads as a stronger claim than
     "here's a hand-curated, mostly-unverified label" should. No card
     background tint either, for the same reason: this is a footnote, not
     a badge of honor or a warning label. */
  .pill.lean {{
    display: inline-flex; align-items: center; gap: .4rem;
    background: none; border: 1px solid var(--line-2); color: var(--sub);
    text-transform: none; letter-spacing: .08em; font-weight: 400;
  }}
  .pill.lean::before {{
    content: ""; width: .32rem; height: .32rem; border-radius: 50%; background: var(--sub); flex: none;
  }}
  /* h2/.snip/.also get flex:none -- .body's children are flex items by
     default (flex-shrink:1), so without this a short .stage/.card height
     could squeeze a clamped h2 shorter than its own 3-line-clamp content
     box, clipping mid-glyph instead of at a clean line boundary (the
     headline visibly overlapping the summary below it). .media is the
     one flexible element instead: it grows to fill whatever's left over
     on a short card (region-tag-only, no "also") and shrinks first, down
     to its min-height floor, when text needs the room. */
  .card h2 {{
    margin: 0 0 .4rem; font-family: var(--font-serif); font-size: 1.4rem; line-height: 1.18;
    font-weight: 800; letter-spacing: -.02em;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
    flex: none;
  }}
  .also {{ font-size: .72rem; color: var(--sub); margin: .7rem 0 0; padding-top: .6rem; border-top: 1px solid var(--line); font-style: italic; flex: none; }}
  .also b {{ color: var(--ink); font-weight: 650; font-style: normal; }}
  .snip {{
    margin: 0; color: var(--sub); font-size: .875rem; line-height: 1.52;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
    flex: none;
  }}
  .read {{
    display: inline-flex; align-items: center; gap: .35rem;
    font-family: var(--font-sans); font-weight: 700; font-size: .72rem; letter-spacing: .1em; text-transform: uppercase;
    border-bottom: 2px solid var(--accent); padding-bottom: .1rem;
    color: var(--ink); text-decoration: none;
  }}
  .read svg {{ width: .75rem; height: .75rem; }}
  .read:hover {{ color: var(--accent); text-decoration: none; }}

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

  /* All five controls (Back/Save/Share/Next/Unread) are real, separate,
     always-present buttons at every breakpoint -- distinct DOM per
     control, never relabeled between breakpoints. Only the mobile media
     query below changes the layout (row -> full-width 4-up grid, Next
     hidden since forward stays drag-only on phones); it never changes
     which control does what. */
  .ctrls {{
    display: flex; align-items: center; justify-content: center; gap: .6rem;
    margin: 1rem auto 0; position: relative; z-index: 2; width: fit-content;
  }}
  .rnd {{
    display: inline-flex; align-items: center; justify-content: center;
    padding: .68rem .9rem; min-height: 2.3rem;
    border-radius: 2px; border: 1px solid var(--ink); background: transparent; color: var(--ink);
    cursor: pointer;
    font-family: var(--font-sans); font-size: .68rem; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
    transition: transform .18s var(--spring), background .2s, color .2s;
  }}
  .rnd svg {{ display: none; }}
  .rnd-label {{ font: inherit; letter-spacing: inherit; }}
  .rnd:hover:not(:disabled) {{ background: var(--ink); color: var(--bg); }}
  .rnd:active:not(:disabled) {{ transform: scale(.94); }}
  .rnd:disabled {{ opacity: .4; cursor: default; }}
  .rnd:disabled {{ opacity: .35; cursor: default; }}
  .rnd.on {{ background: var(--ink); border-color: var(--ink); color: var(--bg); }}
  .rnd.on:hover {{ color: var(--bg); border-color: var(--ink); }}
  .rnd.pop {{ animation: pop .42s var(--spring); }}
  .rnd.solid {{ background: var(--ink); border-color: var(--ink); color: var(--bg); }}
  .rnd.solid:hover {{ color: var(--bg); border-color: var(--ink); opacity: .85; }}
  .ctrls-sep {{ width: 1px; align-self: stretch; background: var(--line-2); margin: 0 .3rem; }}
  .count {{ text-align: center; color: var(--sub); font-size: .78rem; margin-bottom: .7rem; font-variant-numeric: tabular-nums; }}
  .deck-status {{
    display: flex; align-items: baseline; justify-content: center; gap: .6rem;
    font-family: var(--font-mono); font-size: .68rem; letter-spacing: .08em; text-transform: uppercase; color: var(--sub);
    margin-bottom: .6rem; font-variant-numeric: tabular-nums;
  }}
  .deck-status .sep {{ width: 1px; height: .65rem; background: var(--line-2); }}
  .deck-status:empty {{ display: none; }}

  .queue {{ display: none; }}
  .queue-head {{
    display: flex; align-items: baseline; justify-content: space-between;
    margin: 0 0 .7rem; padding-bottom: .55rem; border-bottom: 3px double var(--ink);
  }}
  .queue h3 {{ font-family: var(--font-serif); font-size: 1.05rem; font-weight: 800; color: var(--ink); margin: 0; }}
  .queue-head span {{ font-family: var(--font-mono); font-size: .64rem; letter-spacing: .1em; text-transform: uppercase; color: var(--sub); }}
  .qi {{
    display: flex; gap: .7rem; align-items: flex-start; width: 100%; text-align: left;
    padding: .55rem .1rem; border-radius: 0; border: 0; border-bottom: 1px solid var(--line);
    background: none; font: inherit; color: inherit; cursor: pointer;
    animation: chipIn .4s var(--out) both; animation-delay: calc(var(--i, 0) * 40ms);
    transition: background .16s, transform .16s var(--out);
  }}
  .qi:hover {{ background: var(--glass-2); transform: translateX(2px); }}
  .qi .thumb {{
    flex: none; width: 3.2rem; height: 3.2rem; display: grid; place-items: center;
    border: 1px solid var(--line-2); background: var(--bg-2); overflow: hidden;
    font-family: var(--font-serif); font-weight: 800; font-size: .95rem; color: var(--sub);
  }}
  .qi .thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .qi .t {{ font-size: .81rem; font-weight: 640; line-height: 1.34; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
  .qi .s {{ font-family: var(--font-sans); font-size: .64rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--sub); margin-bottom: .3rem; }}
  .qempty {{ color: var(--sub); font-size: .8rem; }}

  .toast {{
    position: fixed; left: 50%; z-index: 60;
    bottom: calc(1.4rem + env(safe-area-inset-bottom));
    transform: translate(-50%, 24px) scale(.96); opacity: 0; pointer-events: none;
    display: flex; align-items: center; gap: .45rem;
    padding: .6rem 1rem; border-radius: 0; border: 1px solid var(--line);
    background: var(--glass); color: var(--ink);
    font-family: var(--font-sans); font-size: .78rem; font-weight: 700; letter-spacing: .04em; box-shadow: var(--shadow-xl);
    transition: opacity .28s var(--out), transform .38s var(--spring);
  }}
  .toast.show {{ opacity: 1; transform: translate(-50%, 0) scale(1); }}

  :focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 8px; }}

  /* Edge-to-edge card on phones -- closer to how Inshorts et al. do it, and
     it removes the ~6% side gutter that made the deck read as "a card
     floating in the middle of the page" instead of one full-width surface.
     Scoped to phone widths only: at tablet widths (641-899px) the floating,
     rounded card still has room to breathe and looks intentional there. */
  @media (max-width: 640px) {{
    .layout {{ padding-left: 0; padding-right: 0; }}
    .card {{ width: 100%; border-radius: 0; border-width: 1px 0; }}
  }}

  /* Below the desktop breakpoint, stop clamping .stage to a fixed height
     and let it fill whatever's actually left of the viewport -- the old
     `clamp(380px, 54vh, 500px)` capped the card at roughly half the
     screen on tall phones, leaving a dead black gap below the controls.
     Scoped to this media query only (not the base `body` rule above) so
     desktop's landscape layout, which relies on content sizing itself,
     is untouched. The SIZE slider is hidden here too -- once the card
     already fills the screen there's nothing left to scale it into. */
  @media (max-width: 899px) {{
    /* svh, not dvh -- dvh reports the *expanded*-toolbar height, which is
       briefly taller than what's actually on screen while Safari's address
       bar is still showing on load. That gap made the page a few px taller
       than the visible area, and iOS happily treats that sliver as
       rubber-band-scrollable even with overflow:hidden set -- a drag
       revealed a strip of the next peeking card and a slab of black below
       it. svh is the guaranteed-visible floor (toolbar fully expanded), so
       the column never exceeds what's on screen in the first place.
       overscroll-behavior is a second lock against any residual bounce. */
    html {{ height: 100%; overflow: hidden; }}
    body {{
      height: 100vh; height: 100svh; display: flex; flex-direction: column;
      overflow: hidden; overscroll-behavior: none;
    }}
    header {{ flex: none; }}
    /* margin: 0 auto in the base rule (desktop centering) forces auto-margin
       shrink-to-fit under flex, overriding stretch -- kill it here so the
       column actually fills the full width instead of floating narrow and
       centered. */
    .layout {{
      flex: 1; min-height: 0; display: flex; flex-direction: column; align-items: stretch;
      margin: 0; max-width: none; padding: 0;
    }}
    .layout > div:first-child {{ flex: 1; min-height: 0; display: flex; flex-direction: column; }}
    .stage {{ flex: 1; min-height: 0; height: auto; }}
    .count {{ flex: none; }}
    /* Forward nav is already the drag gesture (either direction advances --
       see attachDrag), so Next earns no space here; Back/Save/Share/Unread
       become a full-width, always-visible 4-up bar instead of a pill
       floating below the card -- distinct DOM per control, same buttons as
       desktop, just laid out differently and with Next hidden. The 1px
       gap + var(--line-2) background on the grid, with each button's own
       background painted over it, draws hairline dividers between the four
       cells for free. */
    .ctrls {{
      flex: none; display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px;
      width: 100%; margin: 0; background: var(--line-2); border-top: 1px solid var(--ink);
      padding-bottom: env(safe-area-inset-bottom);
    }}
    .rnd {{ border: none; background: var(--bg); min-width: 0; height: 3.8rem; padding: .3rem; }}
    .rnd.solid {{ background: var(--ink); }}
    #next, .ctrls-sep {{ display: none; }}
  }}

  /* Landscape phones: the mobile portrait card forces a >=380px-tall stage
     (see .stage above), which overflows a short landscape viewport and
     shoves/overlaps the prev/next buttons below it. Below the 900px
     tablet/desktop width, fall back to the same short, side-by-side card
     layout whenever the viewport itself is short, regardless of width. */
  @media (min-width: 900px), (max-height: 480px) {{
    /* Wider editorial card, still a single text-first column with the lead
       image as a band near the bottom -- not a side-by-side layout. */
    .stage {{ height: clamp(420px, 62vh, 620px); margin-bottom: 2rem; }}
    .card {{ width: min(94%, 780px); }}
    .body {{ padding: 1.9rem 2.2rem 1.6rem; }}
    .card h2 {{ font-size: clamp(1.9rem, 2.6vw, 2.6rem); line-height: 1.08; }}
    .snip {{ font-size: 1.05rem; line-height: 1.55; }}
    .media {{ min-height: 9rem; }}

    .ctrls {{ gap: .5rem; margin-top: 1rem; }}
  }}

  @media (min-width: 900px) {{
    header {{ border-bottom: 3px double var(--ink); }}
    .layout {{ grid-template-columns: minmax(0, 1fr) 296px; gap: 2.25rem; }}
    .queue {{ display: block; position: sticky; top: 8.5rem; }}
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
      <button class="menu-btn" id="menu-btn" aria-label="Open filters" aria-expanded="false" aria-controls="drawer">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
          <line class="ln ln1" x1="4" y1="7" x2="20" y2="7"/>
          <line class="ln ln2" x1="4" y1="14" x2="20" y2="14"/>
          <line class="ln ln3" x1="4" y1="17" x2="20" y2="17"/>
        </svg>
      </button>
      <div class="brand">
        <img class="brand-mark" src="icon-192.png" alt="" width="192" height="192">
        NewsFlick
      </div>
    </div>
    <div class="rail"><i id="rail"></i></div>
  </header>

  <div class="scrim" id="scrim"></div>
  <aside class="drawer" id="drawer" aria-label="Filters">
    <div class="drawer-head">
      <strong>Filters</strong>
      <button class="drawer-close" id="drawer-close" aria-label="Close filters">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>
    <div class="drawer-body">
      <div class="section-head" style="cursor:default">
        <span class="t">Appearance</span>
      </div>
      <div class="theme-switch" id="theme-switch" role="group" aria-label="Theme">
        <button class="theme-opt" data-theme="auto">Auto</button>
        <button class="theme-opt" data-theme="light">Light</button>
        <button class="theme-opt" data-theme="dark">Dark</button>
      </div>
      <div class="section" id="section-topics">
        <button class="section-head" data-toggle="section-topics">
          <span class="t">Interests</span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
        </button>
        <div class="section-panel"><div>
          <p class="section-hint">Pick as many as you like -- leave empty to see everything.</p>
          <nav class="chipwrap" id="topics" aria-label="Filter by topic"></nav>
        </div></div>
      </div>
      <div class="section" id="section-regions">
        <button class="section-head" data-toggle="section-regions">
          <span class="t">World</span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
        </button>
        <div class="section-panel"><div>
          <nav class="chipwrap" id="regions" aria-label="Filter by region"></nav>
        </div></div>
      </div>
      <div class="section" id="section-sources">
        <button class="section-head" data-toggle="section-sources">
          <span class="t">Sources</span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
        </button>
        <div class="section-panel"><div>
          <nav class="chipwrap" id="sources" aria-label="Filter by source"></nav>
        </div></div>
      </div>
      <p class="drawer-footnote">Leaning shown on cards is from public ratings where available, hand-curated
        (not an API) -- most sources aren't independently rated.</p>
      <p class="drawer-fresh" id="fresh"></p>
      <button class="drawer-show" id="drawer-show" type="button">Show <span id="drawer-show-count"></span> stories</button>
      <p class="drawer-keys">Keyboard: &larr; back &middot; &rarr; next &middot; S save</p>
    </div>
  </aside>

  <div class="layout">
    <div>
      <div class="deck-status" id="deck-status"></div>
      <main class="stage" id="stage" aria-live="polite"></main>
      <div class="count" id="count"></div>
      <nav class="ctrls" aria-label="Article controls">
        <button class="rnd" id="prev" title="Previous (left arrow)" aria-label="Previous story">
          <span class="rnd-label">&larr; Back</span>
        </button>
        <button class="rnd" id="ctrl-save" aria-label="Save for later" aria-pressed="false">
          <span class="rnd-label" id="ctrl-save-label">Save</span>
        </button>
        <button class="rnd" id="ctrl-share" aria-label="Share story">
          <span class="rnd-label">Share</span>
        </button>
        <button class="rnd" id="next" title="Next (right arrow)" aria-label="Next story">
          <span class="rnd-label">Next &rarr;</span>
        </button>
        <span class="ctrls-sep" aria-hidden="true"></span>
        <button class="rnd solid" id="refresh" aria-label="Jump to an unread story">
          <span class="rnd-label">&#8635; Something unread</span>
        </button>
      </nav>
    </div>
    <aside class="queue" id="queue">
      <div class="queue-head"><h3>Up next</h3><span>in the pile</span></div>
      <div id="qlist"></div>
    </aside>
  </div>

  <div class="toast" id="toast" role="status" aria-live="polite"></div>

  <div class="onb-scrim" id="onb-scrim">
    <div class="onb" role="dialog" aria-modal="true" aria-labelledby="onb-title">
      <h2 id="onb-title">What do you want to see?</h2>
      <p>Pick a few topics to lead with. You can change this anytime from the filter drawer.</p>
      <nav class="chipwrap" id="onb-topics" aria-label="Choose topics"></nav>
      <div class="onb-actions">
        <button class="onb-skip" id="onb-skip">Skip, show everything</button>
        <button class="onb-go" id="onb-go">Save preferences</button>
      </div>
    </div>
  </div>

  <div class="onb-scrim" id="a2hs-scrim">
    <div class="onb a2hs" role="dialog" aria-modal="true" aria-labelledby="a2hs-title">
      <button class="a2hs-close" id="a2hs-close" aria-label="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
      <h2 id="a2hs-title">Add NewsFlick to your Home Screen</h2>
      <p>Full-screen, no browser bar, one tap to open.</p>
      <ol class="a2hs-steps">
        <li>Tap the <b>Share</b> button in Safari's toolbar</li>
        <li>Scroll down and tap <b>Add to Home Screen</b></li>
        <li>Tap <b>Add</b> in the top-right corner</li>
      </ol>
    </div>
  </div>

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
    var regionsEl = document.getElementById('regions');
    var onbTopicsEl = document.getElementById('onb-topics');
    var qlist = document.getElementById('qlist');
    var countEl = document.getElementById('count');
    var railEl = document.getElementById('rail');
    var freshEl = document.getElementById('fresh');
    var deckStatusEl = document.getElementById('deck-status');
    var toastEl = document.getElementById('toast');
    var prevBtn = document.getElementById('prev');
    var nextBtn = document.getElementById('next');
    var ctrlSaveBtn = document.getElementById('ctrl-save');
    var ctrlSaveLabelEl = document.getElementById('ctrl-save-label');
    var ctrlShareBtn = document.getElementById('ctrl-share');
    var menuBtn = document.getElementById('menu-btn');
    var drawerEl = document.getElementById('drawer');
    var scrimEl = document.getElementById('scrim');

    var SAVED_KEY = 'newsdigest:saved';
    var saved = new Set();
    try {{
      var raw = JSON.parse(localStorage.getItem(SAVED_KEY) || '[]');
      if (Array.isArray(raw)) saved = new Set(raw);
    }} catch (e) {{}}

    // sessionStorage, not localStorage: "seen this session" should reset
    // when the tab/browser session actually ends, not persist forever like
    // saved articles do. Refresh (a reload of the same page, same session)
    // keeps it -- that's the point: land on the first article you haven't
    // gotten to yet instead of restarting from the top every time.
    var SEEN_KEY = 'newsdigest:seen';
    var seen = new Set();
    try {{
      var rawSeen = JSON.parse(sessionStorage.getItem(SEEN_KEY) || '[]');
      if (Array.isArray(rawSeen)) seen = new Set(rawSeen);
    }} catch (e) {{}}
    function markSeen(id) {{
      if (seen.has(id)) return;
      seen.add(id);
      try {{ sessionStorage.setItem(SEEN_KEY, JSON.stringify(Array.from(seen))); }} catch (e) {{}}
    }}

    // Interests are multi-select ("as many topics as you like"), unlike the
    // single-select source filter -- an empty set means no topic filtering at
    // all, not "match nothing".
    var INTERESTS_KEY = 'newsdigest:interests';
    var ONBOARDED_KEY = 'newsdigest:onboarded';
    var interests = new Set();
    try {{
      var rawI = JSON.parse(localStorage.getItem(INTERESTS_KEY) || '[]');
      if (Array.isArray(rawI)) interests = new Set(rawI);
    }} catch (e) {{}}

    function persistInterests() {{
      try {{ localStorage.setItem(INTERESTS_KEY, JSON.stringify(Array.from(interests))); }} catch (e) {{}}
    }}

    var sources = Array.from(new Set(all.map(function (a) {{ return a.source; }}))).sort();
    var topics = Array.from(new Set(all.map(function (a) {{ return a.topic; }}))).sort();
    var regions = Array.from(new Set(all.map(function (a) {{ return a.region; }}))).sort();

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
    // Same idea for interests: a topic that no longer appears in this
    // snapshot would otherwise sit in the set forever, filtering nothing.
    (function pruneInterests() {{
      var live = new Set(topics);
      var kept = Array.from(interests).filter(function (t) {{ return live.has(t); }});
      if (kept.length !== interests.size) {{ interests = new Set(kept); persistInterests(); }}
    }})();

    var sourceFilter = 'all';
    var regionFilter = 'all';
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
      if (interests.size) list = list.filter(function (a) {{ return interests.has(a.topic); }});
      if (regionFilter !== 'all') list = list.filter(function (a) {{ return a.region === regionFilter; }});
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

    // labelHtml is raw HTML (callers pass pre-escaped text or literal icon
    // markup like ICON.star) and goes in unescaped -- val is the filter
    // value shown back in a data attribute, not display text, and always
    // goes through esc(). Two different escaping rules on two adjacent
    // params is exactly the kind of thing that reads as a mistake to the
    // next person touching this; it isn't, but say so.
    function chip(labelHtml, attr, val, on, i, disabled) {{
      return '<button class="chip' + (on ? ' on' : '') + '" ' + attr + '="' + esc(val) + '"'
        + ' style="--i:' + i + '"'
        + (disabled ? ' disabled' : '')
        + ' aria-pressed="' + (on ? 'true' : 'false') + '">' + labelHtml + '</button>';
    }}

    // Renders the same multi-select topic chips into any container (the
    // drawer's #topics and the onboarding form's #onb-topics both use this,
    // so toggling in either place has to re-render both).
    function renderInterestChipsInto(el) {{
      if (!el) return;
      var h = chip('All topics', 'data-t', 'all', interests.size === 0, 0);
      topics.forEach(function (t, i) {{
        h += chip(esc(t), 'data-t', t, interests.has(t), i + 1);
      }});
      el.innerHTML = h;
    }}

    function renderTopics() {{
      renderInterestChipsInto(topicsEl);
      renderInterestChipsInto(onbTopicsEl);
    }}

    function toggleInterest(topic) {{
      if (topic === 'all') {{ interests.clear(); }}
      else if (interests.has(topic)) {{ interests.delete(topic); }}
      else {{ interests.add(topic); }}
      persistInterests();
      index = 0;
      renderTopics();
      renderSources();
      render();
    }}

    // Only well-defined when browsing exactly one topic, or one specific
    // source with no topic narrowing -- "next section" doesn't mean anything
    // when the view is unfiltered or spans several topics at once.
    function nextSectionSuggestion() {{
      function matchesOtherFilter(a) {{
        if (regionFilter !== 'all' && a.region !== regionFilter) return false;
        if (sourceFilter === 'all') return true;
        if (sourceFilter === '__saved__') return saved.has(a.id);
        return a.source === sourceFilter;
      }}
      if (interests.size === 1) {{
        var current = Array.from(interests)[0];
        var start = topics.indexOf(current);
        for (var k = 1; k <= topics.length; k++) {{
          var cand = topics[(start + k) % topics.length];
          if (cand === current) break;
          var count = all.filter(function (a) {{ return a.topic === cand && matchesOtherFilter(a); }}).length;
          if (count > 0) return {{ kind: 'topic', value: cand, count: count, doneLabel: current }};
        }}
        return null;
      }}
      if (interests.size === 0 && sourceFilter !== 'all' && sourceFilter !== '__saved__') {{
        var curS = sourceFilter;
        var startS = sources.indexOf(curS);
        for (var j = 1; j <= sources.length; j++) {{
          var candS = sources[(startS + j) % sources.length];
          if (candS === curS) break;
          var countS = all.filter(function (a) {{
            return a.source === candS && (regionFilter === 'all' || a.region === regionFilter);
          }}).length;
          if (countS > 0) return {{ kind: 'source', value: candS, count: countS, doneLabel: curS }};
        }}
        return null;
      }}
      // Browsing a specific region ("World" filter): once it's exhausted,
      // cycle to the next region that actually has something left under
      // the current topic/source filters, instead of just reporting
      // "you've seen everything" with nowhere to go.
      if (regionFilter !== 'all') {{
        var curR = regionFilter;
        var startR = regions.indexOf(curR);
        for (var k = 1; k <= regions.length; k++) {{
          var candR = regions[(startR + k) % regions.length];
          if (candR === curR) break;
          var countR = all.filter(function (a) {{
            if (a.region !== candR) return false;
            if (interests.size && !interests.has(a.topic)) return false;
            if (sourceFilter === '__saved__') return saved.has(a.id);
            return sourceFilter === 'all' || a.source === sourceFilter;
          }}).length;
          if (countR > 0) return {{ kind: 'region', value: candR, count: countR, doneLabel: curR }};
        }}
        return null;
      }}
      return null;
    }}

    // A source with zero articles under the current topic/region filters
    // would just open onto "Nothing matches those filters" -- grey it out
    // instead of leaving a live-looking button that leads nowhere. Sorted
    // to the back so the pickable sources stay together up front.
    function renderSources() {{
      function matchesOther(a) {{
        if (interests.size && !interests.has(a.topic)) return false;
        if (regionFilter !== 'all' && a.region !== regionFilter) return false;
        return true;
      }}
      var savedCount = all.filter(function (a) {{ return saved.has(a.id) && matchesOther(a); }}).length;
      var counts = {{}};
      sources.forEach(function (s) {{ counts[s] = 0; }});
      all.forEach(function (a) {{ if (matchesOther(a) && a.source in counts) counts[a.source]++; }});
      var ordered = sources.slice().sort(function (a, b) {{
        var da = counts[a] === 0, db = counts[b] === 0;
        return da === db ? 0 : da ? 1 : -1;
      }});
      var h = chip('All', 'data-f', 'all', sourceFilter === 'all', 0);
      h += chip(ICON.star, 'data-f', '__saved__', sourceFilter === '__saved__', 1, savedCount === 0 && sourceFilter !== '__saved__');
      ordered.forEach(function (s, i) {{
        h += chip(esc(s), 'data-f', s, sourceFilter === s, i + 2, counts[s] === 0 && sourceFilter !== s);
      }});
      sourcesEl.innerHTML = h;
      // The star glyph inside a chip must not swallow the click target.
      sourcesEl.querySelectorAll('.chip svg').forEach(function (s) {{
        s.style.width = '.72rem'; s.style.height = '.72rem'; s.style.verticalAlign = '-.1em';
      }});
    }}

    // Continent/region -- orthogonal to Interests (subject) and Sources
    // (publisher). Single-select like Sources: a story is set in one place.
    function renderRegions() {{
      var h = chip('All', 'data-r', 'all', regionFilter === 'all', 0);
      regions.forEach(function (r, i) {{
        h += chip(esc(r), 'data-r', r, regionFilter === r, i + 1);
      }});
      regionsEl.innerHTML = h;
    }}

    /* ---------- cards ---------- */

    function cardMarkup(a) {{
      var img = a.image ? safeUrl(a.image) : '';
      // draggable="false" plus the CSS user-drag/user-select rules on .media
      // img stop the browser's own "drag this image" / text-selection
      // gesture from grabbing a pointerdown that started over the photo --
      // without it, starting a swipe on the image dragged/selected the
      // picture instead of moving the card. Lives after the headline/summary
      // now, matching the "lead image" placement in the mockup -- text reads
      // first, the photo is a footnote to it, not a masthead.
      var media = img
        ? '<div class="media"><img alt="" draggable="false" loading="lazy" decoding="async" referrerpolicy="no-referrer" src="' + esc(img) + '"><div class="scrim"></div><span class="media-cap">lead image &middot; from feed</span></div>'
        : '';
      var also = (a.alsoFrom && a.alsoFrom.length)
        ? '<div class="also">also on <b>' + a.alsoFrom.map(esc).join('</b>, <b>') + '</b></div>'
        : '';
      var link = safeUrl(a.link);
      // "Not rated" never renders a pill -- an unrated source must not look
      // like a deliberate "neutral" judgment.
      var leanTitle = a.citeName ? 'via ' + a.citeName : 'Self-curated, not from an API';
      var lean = (a.leaning && a.leaning !== 'Not rated')
        ? '<span class="pill lean" title="' + esc(leanTitle) + '">' + esc(a.leaning) + '</span>'
        : '';
      // "General" means the classifier found nothing, not that "General"
      // is itself a real topic -- showing a pill that says nothing on
      // roughly half the deck trained people to ignore all the pills.
      // The four registration-mark dots after it are a purely decorative
      // print-masthead flourish (K/Y/M/C), same on every card.
      var topicPill = (a.topic && a.topic !== 'General')
        ? '<span class="pill">' + esc(a.topic) + '</span>'
          + '<span class="regmarks" aria-hidden="true"><i></i><i></i><i></i><i></i></span>'
        : '';
      var region = a.region ? '<span class="region-tag">' + esc(a.region) + '</span>' : '';
      // Read link lives top-right of the metarow, replacing the per-card
      // timestamp (redundant with the drawer's page-freshness line) -- a
      // fixed, always-visible corner action instead of something you have
      // to scroll a variable-length card to find.
      return ''
        + '<div class="body">'
        +   '<div class="metarow">'
        +     '<span class="src"><span class="ava">' + esc(a.initials) + '</span>' + esc(a.source) + '</span>'
        +     (link ? '<a class="read" href="' + esc(link) + '" target="_blank" rel="noopener noreferrer">Read full article' + ICON.out + '</a>' : '')
        +   '</div>'
        +   '<div class="topicrow">'
        +     topicPill
        +     lean
        +     region
        +   '</div>'
        +   '<h2>' + esc(a.title) + '</h2>'
        +   '<p class="snip">' + esc(a.snippet) + '</p>'
        +   media
        +   also
        + '</div>';
    }}

    // Shared by the full rebuild and the lightweight promotion path so a
    // freshly-created card and a promoted one end up in an identical state.
    function buildCardEl(a, stackI) {{
      var el = document.createElement('article');
      el.className = 'card' + (stackI === 0 ? ' top' : '');
      el.style.setProperty('--i', stackI);
      el.style.zIndex = String(10 - stackI);
      el.dataset.id = a.id;
      el.innerHTML = cardMarkup(a);
      if (stackI > 0) {{
        el.style.transform = 'translateY(' + (stackI * 11) + 'px) scale(' + (1 - stackI * 0.045) + ')';
        el.style.opacity = stackI === 2 ? '.55' : '.85';
        el.setAttribute('aria-hidden', 'true');
      }} else {{
        attachDrag(el);
        markSeen(a.id);
      }}
      var im = el.querySelector('.media img');
      if (im) bindImage(im);
      return el;
    }}

    function paintChrome(list) {{
      var pct = list.length ? Math.min(100, (index / list.length) * 100) : 0;
      railEl.style.width = pct + '%';
      prevBtn.disabled = index === 0;
      nextBtn.disabled = index >= list.length;
      var cur = list[index];
      var curSaved = !!(cur && saved.has(cur.id));
      ctrlSaveBtn.classList.toggle('on', curSaved);
      ctrlSaveBtn.setAttribute('aria-pressed', curSaved ? 'true' : 'false');
      ctrlSaveLabelEl.textContent = curSaved ? 'Saved' : 'Save';
      deckStatusEl.innerHTML = (list.length && index < list.length)
        ? 'Story ' + (index + 1) + ' of ' + list.length + '<span class="sep"></span>drag either way = next'
        : '';
    }}

    // Full rebuild: destroys and recreates the whole stack. Correct after
    // anything that can change WHICH articles are in play (filter change,
    // deep link, restart, going backward) -- but rebuilding on every forward
    // step is what caused the swipe hiccup: the card being promoted from
    // "peeking behind" to "on top" popped straight to its final position
    // instead of animating there, since a freshly-created element has
    // nothing to transition *from*. promoteAfterFlyOut() below handles the
    // forward-step case by mutating the existing DOM nodes instead.
    function render() {{
      var list = filtered();
      clampIndex();
      stage.innerHTML = '';
      busy = false;
      renderSeq++;
      paintChrome(list);

      if (index >= list.length) {{
        countEl.textContent = list.length ? 'End of the queue' : 'Nothing matches those filters';
        var end = document.createElement('article');
        end.className = 'card end';
        var next = list.length ? nextSectionSuggestion() : null;
        // The tall centered end-card leaves a lot of dead space above the
        // checkmark -- the print-registration dots fill it instead of
        // leaving it blank.
        var marks = '<span class="regmarks regmarks-lg" aria-hidden="true"><i></i><i></i><i></i><i></i></span>';
        if (next) {{
          end.innerHTML = marks + '<div class="big">&#10003;</div><h2>' + esc(next.doneLabel) + ' done</h2>'
            + '<p>More in ' + esc(next.value) + '.</p>'
            + '<button class="onb-go" data-act="next-section" data-kind="' + next.kind + '" data-value="' + esc(next.value) + '">'
            + 'Continue to ' + esc(next.value) + '</button>'
            + '<button class="ghost" data-act="restart" style="margin-top:.6rem">Start over instead</button>';
        }} else {{
          end.innerHTML = list.length
            ? marks + '<div class="big">&#10003;</div><h2>All caught up</h2><p>You have been through every story in this view.</p>'
              + '<button class="ghost" data-act="restart">Start over</button>'
            : marks + '<div class="big">&#9788;</div><h2>Nothing here</h2><p>Try a different topic or source.</p>';
        }}
        stage.appendChild(end);
        renderQueue();
        return;
      }}

      countEl.textContent = '';

      // Paint back-to-front so the top card is last in the DOM.
      var depth = Math.min(3, list.length - index);
      for (var i = depth - 1; i >= 0; i--) {{
        stage.appendChild(buildCardEl(list[index + i], i));
      }}
      renderQueue();
    }}

    // Forward-step path: the top card has already animated off-screen and
    // been removed by the caller. Promote every remaining stacked card up
    // one level in place (a CSS transition on the *same* DOM node, so it
    // actually moves instead of popping in) and append one fresh card at the
    // back if the deck still has one to show.
    function promoteAfterFlyOut() {{
      var list = filtered();
      if (index >= list.length) {{ render(); return; }}   // nothing to promote from

      busy = false;
      renderSeq++;
      paintChrome(list);
      countEl.textContent = '';

      var remaining = Array.prototype.slice.call(stage.querySelectorAll('.card'));
      remaining.forEach(function (el) {{
        var newI = (parseInt(el.style.getPropertyValue('--i'), 10) || 0) - 1;
        el.style.setProperty('--i', newI);
        el.style.zIndex = String(10 - newI);
        if (newI === 0) {{
          el.classList.add('top');
          el.removeAttribute('aria-hidden');
          el.style.transform = '';
          el.style.opacity = '';
          attachDrag(el);
          markSeen(el.dataset.id);
        }} else {{
          el.style.transform = 'translateY(' + (newI * 11) + 'px) scale(' + (1 - newI * 0.045) + ')';
          el.style.opacity = newI === 2 ? '.55' : '.85';
        }}
      }});

      var depth = Math.min(3, list.length - index);
      for (var pos = remaining.length; pos < depth; pos++) {{
        stage.appendChild(buildCardEl(list[index + pos], pos));
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
        var img = a.image ? safeUrl(a.image) : '';
        var thumb = img
          ? '<img alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" src="' + esc(img) + '">'
          : esc(a.initials);
        return '<button class="qi" data-jump="' + esc(a.id) + '" style="--i:' + i + '">'
          + '<span class="thumb">' + thumb + '</span>'
          + '<span><span class="s">' + esc(a.source) + ' &middot; ' + esc(relTime(a.published)) + '</span>'
          + '<span class="t">' + esc(a.title) + '</span></span></button>';
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
        card.remove();   // no-op if a full render() already replaced the stage
        if (seq === renderSeq) {{ index += 1; promoteAfterFlyOut(); }}
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
      ctrlSaveLabelEl.textContent = on ? 'Saved' : 'Save';
      renderSources();
      toast(on ? 'Saved' : 'Removed from saved');
      // Un-saving inside the Saved view shrinks the list under our feet.
      if (sourceFilter === '__saved__') render();
    }}

    // ---- Inshorts-style share image: draw the card's own photo, headline,
    // summary and source into a canvas and export it as a PNG, instead of
    // sharing a bare link. Canvas API only -- no CDN dependency for a static
    // site with no build step.

    var SHARE_W = 1080, SHARE_H = 1350;   // 4:5, plays nicely as a feed post

    // Manual word-wrap: canvas has no auto-wrapping, so measure as we go and
    // ellipsize whatever's left once maxLines is hit.
    function wrapLines(ctx, text, maxWidth, maxLines) {{
      var words = String(text || '').split(/\\s+/).filter(Boolean);
      var lines = [];
      var line = '';
      var i = 0;
      while (i < words.length && lines.length < maxLines) {{
        var test = line ? line + ' ' + words[i] : words[i];
        if (line && ctx.measureText(test).width > maxWidth) {{
          lines.push(line);
          line = '';
        }} else {{
          line = test;
          i++;
        }}
      }}
      if (line && lines.length < maxLines) lines.push(line);
      if (i < words.length && lines.length) {{
        var last = lines[lines.length - 1];
        while (last.length > 1 && ctx.measureText(last + '…').width > maxWidth) {{
          last = last.slice(0, -1);
        }}
        lines[lines.length - 1] = last + '…';
      }}
      return lines;
    }}

    // CSS object-fit:cover, but for a canvas -- crop to the target aspect
    // before scaling so the photo fills the frame with no letterboxing.
    function drawCover(ctx, img, x, y, w, h) {{
      var ir = img.naturalWidth / img.naturalHeight;
      var tr = w / h;
      var sx, sy, sw, sh;
      if (ir > tr) {{
        sh = img.naturalHeight; sw = sh * tr; sx = (img.naturalWidth - sw) / 2; sy = 0;
      }} else {{
        sw = img.naturalWidth; sh = sw / tr; sx = 0; sy = (img.naturalHeight - sh) / 2;
      }}
      ctx.drawImage(img, sx, sy, sw, sh, x, y, w, h);
    }}

    function roundRectPath(ctx, x, y, w, h, r) {{
      ctx.beginPath();
      ctx.moveTo(x + r, y);
      ctx.arcTo(x + w, y, x + w, y + h, r);
      ctx.arcTo(x + w, y + h, x, y + h, r);
      ctx.arcTo(x, y + h, x, y, r);
      ctx.arcTo(x, y, x + w, y, r);
      ctx.closePath();
    }}

    // Decorative only (the exported PNG is a static image) -- mirrors the
    // save/share glyphs on the card itself so the crop still reads as "from
    // this app" once it's out in a chat thread.
    function drawTopRightIcons(ctx, rightEdge, cy) {{
      [{{ dx: 76, path: 'M-9,-2 L9,-2 M0,-9 L0,7 M-6,-3 L0,-9 L6,-3' }},   // share arrow
       {{ dx: 0, path: 'M-8,-9 L8,-9 L8,9 L0,2 L-8,9 Z' }}                // bookmark
      ].forEach(function (icon) {{
        var cx = rightEdge - icon.dx;
        ctx.save();
        ctx.beginPath();
        ctx.arc(cx, cy, 30, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(10,10,14,.4)';
        ctx.fill();
        ctx.translate(cx, cy);
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 2.4; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
        ctx.stroke(new Path2D(icon.path));
        ctx.restore();
      }});
    }}

    // Pulls the live theme's actual colors instead of hardcoding the old
    // dark-glass palette, so the exported PNG matches whatever's on screen
    // (light or dark) rather than a fixed identity the redesign moved past.
    function shareColors() {{
      var cs = getComputedStyle(document.documentElement);
      var v = function (name, fallback) {{
        var val = cs.getPropertyValue(name).trim();
        return val || fallback;
      }};
      return {{
        bg: v('--bg', '#f4f1e8'), ink: v('--ink', '#17150f'),
        sub: v('--sub', '#5c574a'), line: v('--line', '#d8d3c3'),
        accent: v('--accent', '#a33f22'),
      }};
    }}

    // Editorial layout, matching the real card: photo on top (clean, no
    // dark scrim baked over it -- this isn't the old overlay style), then a
    // solid text panel below with the same masthead double-rule, ink
    // avatar + serif initial, and CMYK registration dots as the live UI.
    function buildShareCanvas(a, img) {{
      var W = SHARE_W, H = SHARE_H, pad = 56;
      var canvas = document.createElement('canvas');
      canvas.width = W; canvas.height = H;
      var ctx = canvas.getContext('2d');
      var c = shareColors();
      var photoH = Math.round(H * 0.46);

      ctx.fillStyle = c.bg;
      ctx.fillRect(0, 0, W, H);
      drawCover(ctx, img, 0, 0, W, photoH);
      drawTopRightIcons(ctx, W - pad - 30, pad + 28);

      // Double-rule seam between photo and text panel -- same motif as the
      // masthead border and the "Also covered by" divider in the real card.
      ctx.fillStyle = c.ink;
      ctx.fillRect(0, photoH, W, 3);
      ctx.fillRect(0, photoH + 9, W, 3);

      var y = photoH + 48;

      // Source avatar (ink square, serif initial) + name.
      ctx.fillStyle = c.ink;
      ctx.fillRect(pad, y, 56, 56);
      ctx.fillStyle = c.bg;
      ctx.font = '800 26px Spectral, Georgia, serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(String(a.initials || '').slice(0, 2).toUpperCase(), pad + 28, y + 28);
      ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';

      ctx.fillStyle = c.ink;
      ctx.font = '700 30px Archivo, ui-sans-serif, system-ui, sans-serif';
      ctx.fillText(a.source || '', pad + 74, y + 37);
      y += 56 + 40;

      var topicText = String(a.topic || '').toUpperCase();
      if (topicText) {{
        ctx.font = '700 22px Archivo, ui-sans-serif, system-ui, sans-serif';
        var topicW = ctx.measureText(topicText).width;
        ctx.fillStyle = c.ink;
        ctx.fillRect(pad, y, topicW + 32, 44);
        ctx.fillStyle = c.bg;
        ctx.textBaseline = 'middle';
        ctx.fillText(topicText, pad + 16, y + 23);
        ctx.textBaseline = 'alphabetic';
        // Four print-registration dots, same K/Y/M/C as the card's topic pill.
        var dotX = pad + topicW + 32 + 22, dotY = y + 22;
        [['#17150f', 0], ['#ffd400', 14], ['#ec1c5c', 28], ['#00aeef', 42]].forEach(function (d) {{
          ctx.beginPath(); ctx.fillStyle = d[0];
          ctx.arc(dotX + d[1], dotY, 5, 0, Math.PI * 2); ctx.fill();
        }});
      }}
      y += 44 + 36;

      ctx.font = '800 54px Spectral, Georgia, serif';
      ctx.fillStyle = c.ink;
      var headlineLines = wrapLines(ctx, a.title, W - pad * 2, 3);
      headlineLines.forEach(function (line, i) {{ ctx.fillText(line, pad, y + i * 62); }});
      y += headlineLines.length * 62 + 26;

      ctx.font = '400 32px Spectral, Georgia, serif';
      ctx.fillStyle = c.sub;
      var summaryLines = wrapLines(ctx, a.snippet, W - pad * 2, 2);
      summaryLines.forEach(function (line, i) {{ ctx.fillText(line, pad, y + i * 42); }});

      ctx.font = '600 24px Archivo, ui-sans-serif, system-ui, sans-serif';
      ctx.fillStyle = c.sub;
      ctx.fillText('NewsFlick', pad, H - pad + 4);

      return canvas;
    }}

    function loadImageForCanvas(url) {{
      return new Promise(function (resolve, reject) {{
        var img = new Image();
        img.crossOrigin = 'anonymous';   // needed to read pixels back out for export
        img.referrerPolicy = 'no-referrer';
        img.onload = function () {{ resolve(img); }};
        img.onerror = function () {{ reject(new Error('image failed to load')); }};
        img.src = url;
      }});
    }}

    function canvasToPngFile(canvas, name) {{
      return new Promise(function (resolve, reject) {{
        canvas.toBlob(function (blob) {{
          if (!blob) {{ reject(new Error('toBlob returned null (tainted canvas?)')); return; }}
          resolve(new File([blob], name + '.png', {{ type: 'image/png' }}));
        }}, 'image/png');
      }});
    }}

    function downloadFile(file) {{
      var url = URL.createObjectURL(file);
      var link = document.createElement('a');
      link.href = url; link.download = file.name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () {{ URL.revokeObjectURL(url); }}, 4000);
    }}

    // Old behaviour, kept as the fallback for browsers with no Web Share
    // Level 2 (file) support and for images a canvas can't read back out
    // (see the catch in share() below).
    function shareLinkFallback(a) {{
      var url = location.origin + location.pathname + '#' + a.id;
      var text = a.title + ' — via NewsFlick';
      if (navigator.share) {{
        navigator.share({{ title: a.title, text: text, url: url }}).catch(function () {{}});
      }} else if (navigator.clipboard) {{
        navigator.clipboard.writeText(text + '\\n' + url)
          .then(function () {{ toast('Link copied'); }}, function () {{ toast('Could not copy'); }});
      }} else {{
        toast(url);
      }}
    }}

    function share(a) {{
      var imgUrl = a.image ? safeUrl(a.image) : '';
      if (!imgUrl || !document.createElement('canvas').getContext) {{
        shareLinkFallback(a);
        return;
      }}
      toast('Preparing image…');
      loadImageForCanvas(imgUrl)
        .then(function (img) {{ return canvasToPngFile(buildShareCanvas(a, img), 'newsflick-' + a.id); }})
        .then(function (file) {{
          if (navigator.share && navigator.canShare && navigator.canShare({{ files: [file] }})) {{
            return navigator.share({{ files: [file], title: a.title, text: a.title + ' — via NewsFlick' }})
              .catch(function (e) {{
                // AbortError just means the user dismissed the share sheet.
                if (e && e.name !== 'AbortError') {{ downloadFile(file); toast('Image saved'); }}
              }});
          }}
          downloadFile(file);
          toast('Image saved');
        }})
        .catch(function () {{
          // Most likely a tainted canvas: this source's image host doesn't
          // send Access-Control-Allow-Origin, so the pixels can't be read
          // back out to export. Degrade to the plain link instead of
          // failing silently.
          toast("Can't generate an image for this source — sharing the link instead");
          shareLinkFallback(a);
        }});
    }}

    stage.addEventListener('click', function (e) {{
      var btn = e.target.closest('[data-act]');
      if (!btn) return;
      var act = btn.dataset.act;
      if (act === 'restart') {{ index = 0; render(); return; }}
      if (act === 'next-section') {{
        index = 0;
        if (btn.dataset.kind === 'topic') {{
          interests.clear(); interests.add(btn.dataset.value); persistInterests(); renderTopics();
        }} else if (btn.dataset.kind === 'region') {{
          regionFilter = btn.dataset.value; renderRegions();
        }} else {{
          sourceFilter = btn.dataset.value; renderSources();
        }}
        render();
      }}
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
      toggleInterest(btn.dataset.t);
    }});
    if (onbTopicsEl) {{
      onbTopicsEl.addEventListener('click', function (e) {{
        var btn = e.target.closest('.chip');
        if (!btn) return;
        toggleInterest(btn.dataset.t);
      }});
    }}

    sourcesEl.addEventListener('click', function (e) {{
      var btn = e.target.closest('.chip');
      if (!btn) return;
      sourceFilter = btn.dataset.f;
      index = 0;
      renderSources();
      render();
    }});

    regionsEl.addEventListener('click', function (e) {{
      var btn = e.target.closest('.chip');
      if (!btn) return;
      regionFilter = btn.dataset.r;
      index = 0;
      renderRegions();
      renderSources();
      render();
    }});

    prevBtn.addEventListener('click', goBack);
    nextBtn.addEventListener('click', function () {{
      var top = stage.querySelector('.card.top');
      if (top) flyOut(top, 1); else advance();
    }});
    ctrlSaveBtn.addEventListener('click', function () {{
      var c = stage.querySelector('.card.top');
      if (c) toggleSave(c.dataset.id, ctrlSaveBtn);
    }});
    ctrlShareBtn.addEventListener('click', function () {{
      var c = stage.querySelector('.card.top');
      if (!c) return;
      var a = all.find(function (x) {{ return x.id === c.dataset.id; }});
      if (a) share(a);
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
        if (c) toggleSave(c.dataset.id, ctrlSaveBtn);
      }}
    }});

    /* ---------- deep links ---------- */

    function jumpToHash() {{
      var id = location.hash.slice(1);
      if (!id) return false;
      // Clear filters first: a shared card may not be in the current view.
      if (!all.some(function (a) {{ return a.id === id; }})) return false;
      sourceFilter = 'all'; interests.clear(); regionFilter = 'all';
      index = filtered().findIndex(function (a) {{ return a.id === id; }});
      if (index < 0) index = 0;
      renderTopics(); renderSources(); renderRegions(); render();
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

    // Jumps to a random not-yet-seen article in the current view rather
    // than reloading -- reloading did nothing *visible* most of the time
    // (same build, same articles, still inside the cooldown), which read
    // as "the button doesn't work". Falls back to an actual re-fetch only
    // once there's nothing left unseen to shuffle to; that fetch does NOT
    // kick off a new Actions run -- a static page has nowhere safe to keep
    // a token that could. It just re-fetches whatever the last cron run
    // published, past any stale browser/CDN copy.
    document.getElementById('refresh').addEventListener('click', function () {{
      var btn = this;
      btn.classList.remove('spin'); void btn.offsetWidth; btn.classList.add('spin');

      var list = filtered();
      var unseen = [];
      list.forEach(function (a, i) {{ if (i !== index && !seen.has(a.id)) unseen.push(i); }});
      if (unseen.length) {{
        index = unseen[Math.floor(Math.random() * unseen.length)];
        render();
        toast('Here\\'s something new');
        return;
      }}

      // Nothing unseen left in this exact view -- rather than just saying
      // so, offer the same "next section" a filtered dead-end already
      // offers on the end-of-queue card (topic/source/region cycling).
      var next = nextSectionSuggestion();
      if (next) {{
        index = 0;
        if (next.kind === 'topic') {{ interests.clear(); interests.add(next.value); persistInterests(); renderTopics(); }}
        else if (next.kind === 'region') {{ regionFilter = next.value; renderRegions(); }}
        else {{ sourceFilter = next.value; renderSources(); }}
        render();
        toast('Continuing in ' + next.value);
        return;
      }}

      var m = minsOld();
      if (!isNaN(m) && m < COOLDOWN_MIN) {{
        toast('You\\'ve seen everything in this view');
        return;
      }}
      toast('Fetching the latest…');
      setTimeout(function () {{
        location.replace(location.pathname + '?t=' + Date.now() + location.hash);
      }}, 420);
    }});

    /* ---------- drawer (collapsible left filter panel) ---------- */

    /* ---------- theme switch (Auto follows the OS via the CSS media
       query; Light/Dark set data-theme, which the CSS overrides above key
       off with higher specificity than that media query) ---------- */
    var THEME_KEY = 'newsdigest:theme';
    var themeSwitches = document.querySelectorAll('.theme-switch');
    // The two media-scoped <meta name="theme-color"> tags in <head> already
    // cover "Auto" (they just follow the OS). An explicit Light/Dark pick
    // overrides the CSS via [data-theme], but a <meta> tag can't be scoped
    // to an attribute selector -- so mirror the explicit choice onto both
    // tags directly here, or the browser chrome (status bar/task switcher)
    // would keep following the OS regardless of what the user picked.
    var themeColorMetas = document.querySelectorAll('meta[name="theme-color"]');
    var THEME_COLOR = {{ dark: '#141310', light: '#f4f1e8' }};
    function applyTheme(mode) {{
      if (mode === 'light' || mode === 'dark') document.documentElement.dataset.theme = mode;
      else delete document.documentElement.dataset.theme;
      themeSwitches.forEach(function (sw) {{
        sw.querySelectorAll('.theme-opt').forEach(function (b) {{
          b.classList.toggle('on', b.dataset.theme === mode);
        }});
      }});
      themeColorMetas.forEach(function (m) {{
        if (mode === 'light' || mode === 'dark') {{
          m.setAttribute('content', THEME_COLOR[mode]);
        }} else {{
          m.setAttribute('content', THEME_COLOR[m.media.indexOf('dark') > -1 ? 'dark' : 'light']);
        }}
      }});
    }}
    var savedTheme = 'auto';
    try {{ savedTheme = localStorage.getItem(THEME_KEY) || 'auto'; }} catch (e) {{}}
    applyTheme(savedTheme);
    themeSwitches.forEach(function (sw) {{ sw.addEventListener('click', function (e) {{
      var btn = e.target.closest('.theme-opt');
      if (!btn) return;
      var mode = btn.dataset.theme;
      applyTheme(mode);
      try {{ localStorage.setItem(THEME_KEY, mode); }} catch (e2) {{}}
    }}); }});

    function setDrawer(open) {{
      drawerEl.classList.toggle('open', open);
      scrimEl.classList.toggle('show', open);
      menuBtn.classList.toggle('open', open);
      menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }}
    var drawerShowCountEl = document.getElementById('drawer-show-count');
    menuBtn.addEventListener('click', function () {{
      var open = !drawerEl.classList.contains('open');
      if (open) drawerShowCountEl.textContent = filtered().length;
      setDrawer(open);
    }});
    scrimEl.addEventListener('click', function () {{ setDrawer(false); }});
    document.getElementById('drawer-close').addEventListener('click', function () {{ setDrawer(false); }});
    document.getElementById('drawer-show').addEventListener('click', function () {{ setDrawer(false); }});
    document.addEventListener('keydown', function (e) {{
      if (e.key === 'Escape' && drawerEl.classList.contains('open')) setDrawer(false);
    }});

    // Interests and Sources each collapse independently ("same for both") via
    // a shared data-toggle attribute naming the section id to fold.
    document.querySelectorAll('[data-toggle]').forEach(function (btn) {{
      btn.addEventListener('click', function () {{
        var section = document.getElementById(btn.dataset.toggle);
        if (section) section.classList.toggle('collapsed');
      }});
    }});

    /* ---------- onboarding: "what do you want to see" first-run form ---------- */

    var onbScrim = document.getElementById('onb-scrim');

    function closeOnboarding() {{
      onbScrim.classList.remove('show');
      try {{ localStorage.setItem(ONBOARDED_KEY, '1'); }} catch (e) {{}}
    }}
    document.getElementById('onb-skip').addEventListener('click', function () {{
      interests.clear();
      persistInterests();
      renderTopics();
      render();
      closeOnboarding();
    }});
    document.getElementById('onb-go').addEventListener('click', closeOnboarding);
    onbScrim.addEventListener('click', function (e) {{
      if (e.target === onbScrim) closeOnboarding();   // click on the backdrop itself
    }});

    /* ---------- "add to home screen" nudge (iOS Safari only) ---------- */

    // iOS Safari has no install-banner API (no beforeinstallprompt) --
    // Chrome/Android and desktop get their own native install affordance,
    // so this only needs to cover the one platform with no other path to
    // "add to home screen." navigator.standalone is Safari-only and true
    // once already installed; the display-mode media query is the
    // standards-track equivalent other engines use for the same check.
    var A2HS_KEY = 'newsdigest:a2hs-seen';
    var isIOS = /iP(hone|od|ad)/.test(navigator.userAgent) && !window.MSStream;
    var isStandalone = window.navigator.standalone === true
      || window.matchMedia('(display-mode: standalone)').matches;
    var a2hsScrim = document.getElementById('a2hs-scrim');
    function closeA2HS() {{
      a2hsScrim.classList.remove('show');
      try {{ localStorage.setItem(A2HS_KEY, '1'); }} catch (e) {{}}
    }}
    document.getElementById('a2hs-close').addEventListener('click', closeA2HS);
    a2hsScrim.addEventListener('click', function (e) {{
      if (e.target === a2hsScrim) closeA2HS();
    }});

    /* ---------- boot ---------- */

    if ('serviceWorker' in navigator) {{
      navigator.serviceWorker.register('sw.js').catch(function () {{}});
    }}

    renderTopics();
    renderSources();
    renderRegions();
    if (!jumpToHash()) {{
      // A deep link always wins over this -- someone opening a shared card
      // wants that card, not wherever they left off. Otherwise, land on the
      // first article this session hasn't seen yet (e.g. after tapping
      // Refresh) instead of restarting from the top every time. If
      // everything in view has already been seen, index 0 is still fine --
      // there's nothing unseen to skip to.
      // Against filtered(), not all -- interests/sourceFilter persist across
      // reloads too, so the index has to line up with whatever view that
      // produces, not the unfiltered list.
      var firstUnseen = filtered().findIndex(function (a) {{ return !seen.has(a.id); }});
      if (firstUnseen > 0) index = firstUnseen;
      render();
    }}

    var alreadyOnboarded = false;
    try {{ alreadyOnboarded = !!localStorage.getItem(ONBOARDED_KEY); }} catch (e) {{ alreadyOnboarded = true; }}
    if (!alreadyOnboarded && topics.length) {{
      // Let the deck paint first so the form doesn't block first render.
      setTimeout(function () {{ onbScrim.classList.add('show'); }}, 260);
    }} else {{
      // Never stack this on top of the onboarding form -- a first-time
      // visitor gets that instead, and sees this nudge on a later visit.
      var alreadySeenA2HS = false;
      try {{ alreadySeenA2HS = !!localStorage.getItem(A2HS_KEY); }} catch (e) {{ alreadySeenA2HS = true; }}
      if (isIOS && !isStandalone && !alreadySeenA2HS) {{
        setTimeout(function () {{ a2hsScrim.classList.add('show'); }}, 260);
      }}
    }}
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
        r, arts = check(f["name"], f["url"], f.get("region"))
        r["verdict"] = verdict(r)
        print(f"   {r['verdict']} {r['note']}".rstrip(), flush=True)
        rows.append(r)
        if r["ok"]:
            all_articles.extend(arts)
        time.sleep(1)  # be polite

    live = [r for r in rows if r["verdict"] == "OK"]
    stubs = [r for r in live if r["median_chars"] < STUB_THRESHOLD]
    total_items = sum(r["entries"] for r in live)
    # Distinct from `live` on purpose: `r["ok"]` (parseable entries, 200
    # status) is true for STALE/FUTURE/NO-DATES feeds too, only DEAD sets it
    # false -- and main()'s loop above feeds all_articles from every `ok`
    # row, not just verdict=="OK" ones. Reported separately so "N/M feeds
    # usable" and "articles came from N feeds" don't silently describe two
    # different populations under similar-sounding labels.
    contributing = len([r for r in rows if r["ok"]])

    # Deck-building happens here, before feed_check.md is assembled, so its
    # stats (merges/no-image/deck-limit drops) can land in the report's
    # summary bullets -- they used to only reach print(), which meant they
    # never showed up in the Actions job summary (head -12 feed_check.md).
    deduped = dedupe_articles(all_articles)
    merged = len(all_articles) - len(deduped)
    # No longer dropped. This filter existed because the card used to put
    # the image in a top masthead band -- a missing image left a visibly
    # broken-looking card, so whole categories (Factly, Alt News, MyGov,
    # Economic Times) got silently zeroed out rather than shown with a
    # generated cover (tried, reverted on explicit preference at the time).
    # The card layout changed since: the image now sits below the text as
    # a flexible band, and cardMarkup() already renders cleanly with none
    # (media == ''). A text-only card is a normal card now, not a broken
    # one, so the reason for dropping it is gone.
    no_image = len(deduped) - len([a for a in deduped if a.get("image")])
    deduped.sort(
        key=lambda a: a["published"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    dropped = max(0, len(deduped) - DECK_LIMIT)
    deck = round_robin_by_source(deduped, DECK_LIMIT)
    sourced = len({a["source"] for a in deck})

    out = [
        "# Feed check",
        "",
        f"Run: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        "",
        f"- **{len(live)} / {len(rows)} feeds usable**",
        f"- {total_items} items visible right now across live feeds",
        f"- {len(stubs)} of {len(live)} live feeds are teaser-only "
        f"(<{STUB_THRESHOLD} chars) -> need article extraction",
        f"- {len(all_articles)} articles from {contributing} feeds "
        f"(includes STALE/FUTURE/NO DATES, not just the {len(live)} graded OK) "
        f"-> {len(deduped)} after merging {merged} cross-agency duplicate{'s' if merged != 1 else ''}"
        + (f" ({no_image} have no lead image, shown text-only)" if no_image else ""),
        f"- {sourced} feeds contributed to the {len(deck)}-card deck"
        + (f" ({dropped} older card{'s' if dropped != 1 else ''} not shown, DECK_LIMIT={DECK_LIMIT})" if dropped else ""),
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
    REPORT_HTML.write_text(render_html(deck), encoding="utf-8")

    print(f"\nwrote {REPORT_MD.name} + {REPORT_JSON.name} + {REPORT_HTML.name}")
    print(f"  {len(live)}/{len(rows)} feeds graded OK, {contributing} contributed articles, {sourced} made the deck")
    print(f"  {len(all_articles)} articles -> {len(deduped)} after merging "
          f"{merged} cross-agency duplicate{'s' if merged != 1 else ''}")
    if no_image:
        print(f"  {no_image} card{'s' if no_image != 1 else ''} have no lead image, shown text-only")
    if dropped:
        print(f"  deck capped at {DECK_LIMIT}: {dropped} older card"
              f"{'s' if dropped != 1 else ''} not shown (raise DECK_LIMIT to include them)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
