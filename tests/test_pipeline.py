"""Regression tests for pipeline/check_feeds.py.

Pure functions only -- no network, no fixtures, no feedparser objects beyond
plain dicts (every function under test reads fields via .get()/[...], which
a plain dict satisfies identically to a real FeedParserDict).

Each test below maps to one bullet in README.md's "Audit notes (things
already fixed -- don't reintroduce them)" section. If you fix a bug in
check_feeds.py, add both here: the audit note and the test.
"""

import calendar
import random
import string
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import check_feeds as cf  # noqa: E402


# ---------- entry_time: UTC via calendar.timegm, not local time.mktime ----------

def test_entry_time_uses_utc_not_local():
    # struct_time for 2026-01-01 00:00:00 UTC.
    t = (2026, 1, 1, 0, 0, 0, 3, 1, 0)
    got = cf.entry_time({"published_parsed": t})
    assert got == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    # If this ever used time.mktime instead, the result would be shifted by
    # the local timezone offset (5.5h in IST) -- assert the UTC round trip
    # directly against calendar.timegm to pin the intended behavior.
    assert calendar.timegm(t) == got.timestamp()


def test_entry_time_falls_back_to_updated_parsed():
    t = (2026, 6, 15, 12, 0, 0, 0, 0, 0)
    got = cf.entry_time({"updated_parsed": t})
    assert got == datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_entry_time_none_when_undated():
    assert cf.entry_time({}) is None


# ---------- strip_html: unescape before AND after stripping tags ----------

def test_strip_html_unescapes_before_stripping():
    # Escaped markup ("&lt;p&gt;") must become real tags before stripping,
    # or the literal "<p>" text survives in the output.
    assert cf.strip_html("&lt;p&gt;hello&lt;/p&gt;") == "hello"


def test_strip_html_unescapes_entities_inside_text():
    # "&#8217;" (a right single quote) must decode even after tag-stripping,
    # or it inflates length counts and pollutes downstream text.
    assert cf.strip_html("<p>India&#8217;s economy</p>") == "India’s economy"


def test_strip_html_collapses_whitespace():
    assert cf.strip_html("<p>a</p>\n\n<p>b</p>") == "a b"


# ---------- classify_topic: \b word boundaries, not substrings ----------

def test_classify_topic_who_is_not_health():
    # "who" as a bare substring inside "The MLA who quit the party" used to
    # hit the WHO keyword in Health via substring matching.
    assert cf.classify_topic("The MLA who quit the party", "") != "Health"


def test_classify_topic_ai_is_not_said():
    # A bare "ai" substring-matching inside "said" used to misfire Technology.
    assert cf.classify_topic("Minister said the budget is ready", "") != "Technology"


def test_classify_topic_real_keyword_hits():
    assert cf.classify_topic("Sensex and Nifty close higher amid rate cut hopes", "") == "Business"


def test_classify_topic_url_hint_wins_outright():
    assert cf.classify_topic("Ambiguous headline", "", url_hint="Sports") == "Sports"


# ---------- dedupe_articles: window guard, anchor pinning, also_from ----------

def _article(source, title, published, snippet="", image=None):
    return {
        "source": source, "title": title, "link": f"https://x/{source}/{title[:8]}",
        "published": published, "snippet": snippet, "image": image,
        "topic": "General", "region": "India",
    }


def test_dedupe_merges_near_identical_titles():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    arts = [
        _article("PTI", "Cabinet approves new freight corridor", now),
        _article("ANI", "Cabinet approves new freight corridor project", now + timedelta(minutes=5)),
    ]
    out = cf.dedupe_articles(arts)
    assert len(out) == 1
    assert set(out[0]["also_from"] + [out[0]["source"]]) == {"PTI", "ANI"}


def test_dedupe_window_guard_prevents_cross_day_merge():
    # Same generic headline from the same publisher, 48h apart -- must NOT
    # merge (DEDUPE_WINDOW_HOURS=20 guards exactly this: a recurring
    # headline like "Sensex closes higher" on two different days).
    day1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    day3 = day1 + timedelta(hours=48)
    arts = [
        _article("ET", "Sensex closes higher", day1),
        _article("ET", "Sensex closes higher", day3),
    ]
    out = cf.dedupe_articles(arts)
    assert len(out) == 2


def test_dedupe_anchor_stays_pinned_to_opener():
    # Third article is similar to the *original* opener but not to the
    # better-looking rep that took over -- it must still join the same
    # cluster, not start a second one, because anchor/at stay pinned to
    # whichever article opened the cluster.
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a1 = _article("Src1", "Bengal cricketer Abhishek Porel arrested on charges", now, snippet="short")
    a2 = _article("Src2", "Bengal cricketer Abhishek Porel arrested on rape charges",
                  now + timedelta(minutes=1), snippet="a much longer snippet than the first one here")
    a3 = _article("Src3", "Bengal cricketer Abhishek Porel arrested on charges today",
                  now + timedelta(minutes=2), snippet="mid")
    out = cf.dedupe_articles([a1, a2, a3])
    assert len(out) == 1
    assert out[0]["source"] == "Src2"  # a2 won the rep slot (longer snippet)
    assert set(out[0]["also_from"]) == {"Src1", "Src3"}


def test_dedupe_rep_never_in_its_own_also_from():
    # also_from is derived at the end from members minus the rep's *current*
    # source -- appending as-you-go could leave a source in its own
    # "also on" line once it had been demoted and later won the rep slot back.
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a1 = _article("A", "Government unveils new export policy for textiles", now, snippet="x" * 50)
    a2 = _article("B", "Government unveils new export policy for textiles today",
                  now + timedelta(minutes=1), snippet="x" * 10)
    a3 = _article("A", "Government unveils new export policy for textiles now",
                  now + timedelta(minutes=2), snippet="x" * 100)
    out = cf.dedupe_articles([a1, a2, a3])
    assert len(out) == 1
    assert out[0]["source"] not in out[0]["also_from"]


def test_dedupe_distinct_stories_stay_separate():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    arts = [
        _article("A", "Parliament passes new tribunals reform bill", now),
        _article("B", "Monsoon rainfall forecast revised upward for August", now),
    ]
    out = cf.dedupe_articles(arts)
    assert len(out) == 2


# ---------- dedupe_articles vs the naive O(n^2) reference ----------

_WORDS = (
    "cabinet approves freight corridor rupee inflation cricket test match "
    "monsoon court order metro line election budget bank merger minister "
    "parliament assembly governor election commission farmers protest "
    "startup funding round smartphone launch price cut festival season"
).split()


def _random_articles(n, seed, sources=12, title_words=8):
    rng = random.Random(seed)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        title = " ".join(rng.choices(_WORDS, k=title_words))
        # Occasionally perturb a word or punctuation so near-duplicates with
        # small edits actually occur, not just exact repeats.
        if rng.random() < 0.3:
            title += " " + rng.choice(string.ascii_letters)
        out.append(_article(
            source=f"S{i % sources}",
            title=title,
            published=now + timedelta(minutes=rng.randint(-600, 600)),
            snippet="x" * rng.randint(10, 300),
            image="http://i/x.jpg" if rng.random() < 0.5 else None,
        ))
    return out


def test_dedupe_matches_naive_reference():
    # The indexed dedupe_articles() must produce byte-identical output to
    # the unoptimized _dedupe_articles_naive() reference on every input --
    # the token-index and quick-ratio narrowing are meant to be exact
    # filters, never heuristics. Multiple seeds/sizes to stress the token
    # index's fallback path (short/no-overlap titles) as well as the
    # common case.
    for seed in range(8):
        for n in (0, 1, 5, 40, 150):
            arts = _random_articles(n, seed)
            fast = cf.dedupe_articles(arts)
            naive = cf._dedupe_articles_naive(arts)
            assert fast == naive, f"mismatch at seed={seed} n={n}"


def test_dedupe_matches_naive_reference_with_no_dates():
    # Undated articles (published=None) exercise the "no time-window guard
    # applies" branch on both sides.
    arts = _random_articles(60, seed=1)
    for a in arts[::3]:
        a["published"] = None
    assert cf.dedupe_articles(arts) == cf._dedupe_articles_naive(arts)


# ---------- round_robin_by_source: no single source crowds out the rest ----------

def test_round_robin_caps_high_volume_source():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    arts = [_article("Prolific", f"story {i}", now - timedelta(minutes=i)) for i in range(100)]
    arts += [_article("Quiet", "the one story", now)]
    deck = cf.round_robin_by_source(arts, limit=10)
    sources = [a["source"] for a in deck]
    assert "Quiet" in sources  # would be crowded out by a pure recency sort
    assert len(deck) == 10


def test_round_robin_sorted_newest_first():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    arts = [_article("A", "t1", now - timedelta(hours=2)), _article("B", "t2", now)]
    deck = cf.round_robin_by_source(arts, limit=10)
    assert deck[0]["source"] == "B"


# ---------- entry_image: skip tracking pixels ----------

def test_entry_image_skips_declared_tiny_dimensions():
    entry = {"summary": '<img src="http://x.com/real.jpg" width="800" height="600">'
                        '<img src="http://x.com/pixel.gif" width="1" height="1">'}
    assert cf.entry_image(entry) == "http://x.com/real.jpg"


def test_entry_image_skips_known_pixel_hosts():
    entry = {"summary": '<img src="http://feedburner.com/tracker.gif">'
                        '<img src="http://real-cdn.com/photo.jpg">'}
    assert cf.entry_image(entry) == "http://real-cdn.com/photo.jpg"


def test_entry_image_prefers_media_content():
    entry = {"media_content": [{"url": "http://x.com/a.jpg"}],
             "summary": '<img src="http://x.com/b.jpg">'}
    assert cf.entry_image(entry) == "http://x.com/a.jpg"


def test_entry_image_none_when_nothing_usable():
    assert cf.entry_image({"summary": "no images here"}) is None


# ---------- classify_region: default only applies on zero keyword hits ----------

def test_classify_region_default_only_on_zero_hits():
    assert cf.classify_region("Local body elections announced", "", default_region="India") == "India"


def test_classify_region_keyword_wins_over_default():
    # A domestic feed's story specifically about China must resolve to Asia,
    # not fall back to the feed's India default.
    assert cf.classify_region("PM meets Chinese premier in Beijing", "", default_region="India") == "Asia"
