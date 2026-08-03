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
        articles.append({
            "source": name,
            "title": e.get("title") or "(untitled)",
            "link": entry_link(e),
            "published": t,
            "snippet": (body[:SNIPPET_LEN] + "…") if len(body) > SNIPPET_LEN else body,
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
    gets the same chip color across runs."""
    return (sum(ord(c) for c in name) * 47) % 360


def render_html(articles: list, live_count: int, total_count: int) -> str:
    """Self-contained article listing -- open reports/index.html (or the
    Pages URL) instead of poking at news.db to see what the feeds have."""
    def sort_key(a):
        return a["published"] or datetime.min.replace(tzinfo=timezone.utc)

    cards = []
    for a in sorted(articles, key=sort_key, reverse=True):
        when = f"{a['published']:%Y-%m-%d %H:%M} UTC" if a["published"] else "undated"
        link = html.escape(a["link"], quote=True)
        hue = source_hue(a["source"])
        cards.append(f"""
      <article class="card" style="--hue: {hue}">
        <div class="meta"><span class="chip">{html.escape(a['source'])}</span><time>{when}</time></div>
        <h2><a href="{link}" rel="noopener noreferrer">{html.escape(a['title'])}</a></h2>
        <p class="snippet">{html.escape(a['snippet'])}</p>
      </article>""")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>newsdigest — articles</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #f4f5f7; --surface: #ffffff; --ink: #1a1a1a; --sub: #6b7280;
    --border: rgba(0,0,0,.06); --shadow: 0 1px 3px rgba(0,0,0,.06);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #0e0f11; --surface: #1b1c1f; --ink: #f2f2f2; --sub: #9aa0a6;
             --border: rgba(255,255,255,.08); --shadow: 0 1px 3px rgba(0,0,0,.4); }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--bg); color: var(--ink);
    max-width: 640px; margin: 0 auto; padding: 1.25rem 1rem 3rem;
    line-height: 1.45;
  }}
  header {{ padding: 0.5rem 0.25rem 1.25rem; }}
  header h1 {{ margin: 0; font-size: 1.4rem; letter-spacing: -0.02em; }}
  header p {{ color: var(--sub); margin: 0.3rem 0 0; font-size: 0.85rem; }}
  main {{ display: flex; flex-direction: column; gap: 0.75rem; }}
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 14px; padding: 0.9rem 1.1rem; box-shadow: var(--shadow);
  }}
  .meta {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; }}
  .chip {{
    background: hsl(var(--hue) 70% 92%); color: hsl(var(--hue) 60% 28%);
    padding: 0.15rem 0.6rem; border-radius: 999px;
    font-size: 0.72rem; font-weight: 600; white-space: nowrap;
  }}
  @media (prefers-color-scheme: dark) {{
    .chip {{ background: hsl(var(--hue) 40% 20%); color: hsl(var(--hue) 70% 80%); }}
  }}
  .meta time {{ color: var(--sub); font-size: 0.78rem; }}
  .card h2 {{ font-size: 1.05rem; line-height: 1.3; margin: 0 0 0.35rem; font-weight: 600; }}
  .card a {{ color: inherit; text-decoration: none; }}
  .card a:hover {{ text-decoration: underline; }}
  .snippet {{
    margin: 0; color: var(--sub); font-size: 0.9rem;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    overflow: hidden;
  }}
  .empty {{ color: var(--sub); text-align: center; padding: 3rem 0; }}
</style>
</head>
<body>
  <header>
    <h1>newsdigest</h1>
    <p>Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC &middot; {live_count}/{total_count} feeds usable &middot; {len(articles)} articles</p>
  </header>
  <main>{"".join(cards) if cards else '<p class="empty">No articles fetched.</p>'}
  </main>
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
    REPORT_HTML.write_text(render_html(all_articles, len(live), len(rows)))

    print(f"\nwrote {REPORT_MD.name} + {REPORT_JSON.name} + {REPORT_HTML.name} "
          f"({len(live)}/{len(rows)} usable, {len(all_articles)} articles)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
