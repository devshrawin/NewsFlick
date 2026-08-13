"""Regression tests for pipeline/check_feeds.py.

Pure functions only -- no network, no fixtures, no feedparser objects beyond
plain dicts (every function under test reads fields via .get()/[...], which
a plain dict satisfies identically to a real FeedParserDict).

Each test below maps to one bullet in README.md's "Audit notes (things
already fixed -- don't reintroduce them)" section. If you fix a bug in
check_feeds.py, add both here: the audit note and the test.
"""

import calendar
import json
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


def test_dedupe_known_index_counterexamples():
    # Two real divergences found when the token index was (wrongly) documented
    # as an exact filter. Both scored far above DEDUPE_TITLE_THRESHOLD yet the
    # fast path put them in separate clusters:
    #   1. the cluster opener had no word >3 chars, so it landed in no index
    #      bucket at all and was unreachable from every later article;
    #   2. both titles had long words, but the only ones differed by an
    #      inflection ("rupee"/"rupees"), so they shared no whole-word key.
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for t1, t2 in [
        ("PM to see the new car", "PM to see the new cars"),
        ("Rupee hits new low", "Rupees hit new low"),
        ("Poll date set for May", "Polls date set for May"),
    ]:
        arts = [_article("A", t1, now), _article("B", t2, now + timedelta(minutes=1))]
        fast = cf.dedupe_articles(arts)
        naive = cf._dedupe_articles_naive(arts)
        assert fast == naive, f"index missed a merge the naive path makes: {t1!r} vs {t2!r}"
        assert len(fast) == 1, f"expected these to merge: {t1!r} vs {t2!r}"


_ADVERSARIAL_WORDS = (
    # Deliberately hostile to a whole-word index: short words, inflected
    # pairs, and near-identical stems. The main fuzz vocabulary is all long
    # distinct words, which structurally cannot produce either counterexample
    # above.
    "pm to see the new car cars poll polls rupee rupees hit hits bank banks "
    "low high set date may ban bans tax taxes cut cuts win wins bid bids"
).split()


def test_dedupe_matches_naive_reference_adversarial():
    # Same equivalence assertion as the main fuzz, but over titles built from
    # short/inflected words -- the shape that actually broke the index.
    rng = random.Random(99)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mismatches = []
    for case in range(250):
        n = rng.randint(2, 12)
        arts = []
        for i in range(n):
            title = " ".join(rng.choices(_ADVERSARIAL_WORDS, k=rng.randint(3, 7)))
            arts.append(_article(f"S{i % 4}", title, now + timedelta(minutes=i)))
        if cf.dedupe_articles(arts) != cf._dedupe_articles_naive(arts):
            mismatches.append(case)
    assert not mismatches, f"{len(mismatches)}/250 adversarial cases diverged from the naive reference"


def test_dedupe_matches_naive_reference_with_no_dates():
    # Undated articles (published=None) exercise the "no time-window guard
    # applies" branch on both sides.
    arts = _random_articles(60, seed=1)
    for a in arts[::3]:
        a["published"] = None
    assert cf.dedupe_articles(arts) == cf._dedupe_articles_naive(arts)


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


# ---------- source_bias.yaml: leaning (judgment) vs country/funding (facts) ----------

def _write_bias(tmp_path, monkeypatch, body):
    p = tmp_path / "source_bias.yaml"
    p.write_text(body, encoding="utf-8")
    monkeypatch.setattr(cf, "BIAS_FILE", p)
    return p


def test_load_bias_keeps_facts_only_entries(tmp_path, monkeypatch):
    # The regression this guards: load_bias() used to gate on
    # `entry.get("leaning")` and drop the row otherwise, which silently
    # discarded every country/funding-only entry -- i.e. almost the whole
    # file, since leaning is only set where a citation exists.
    _write_bias(tmp_path, monkeypatch, """
sources:
  Facts Only: {country: India, funding: Private}
""")
    bias = cf.load_bias()
    assert "Facts Only" in bias
    assert bias["Facts Only"]["country"] == "India"
    assert bias["Facts Only"]["funding"] == "Private"
    # No citable rating -> must read as Not rated, never as a "Center" judgment.
    assert bias["Facts Only"]["leaning"] == "Not rated"


def test_load_bias_reads_all_three_fields(tmp_path, monkeypatch):
    _write_bias(tmp_path, monkeypatch, """
sources:
  Full Entry:
    leaning: Center
    cite_name: AllSides
    cite_url: https://example.com/rating
    country: United Kingdom
    funding: Public broadcaster
""")
    e = cf.load_bias()["Full Entry"]
    assert (e["leaning"], e["cite_name"], e["country"], e["funding"]) == (
        "Center", "AllSides", "United Kingdom", "Public broadcaster")


def test_load_bias_skips_entries_with_no_usable_field(tmp_path, monkeypatch):
    _write_bias(tmp_path, monkeypatch, """
sources:
  Empty Entry: {cite_name: Somebody}
  Not A Dict: "just a string"
""")
    assert cf.load_bias() == {}


def test_load_bias_missing_file_and_bad_yaml_are_not_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "BIAS_FILE", tmp_path / "nope.yaml")
    assert cf.load_bias() == {}
    _write_bias(tmp_path, monkeypatch, "sources: [unclosed\n")
    assert cf.load_bias() == {}   # a bad hand-edit must not break the run


def test_real_bias_file_covers_every_feed_and_never_guesses_leaning():
    # Against the real files, not a fixture: every feed should carry the
    # factual tags, and no source may claim a leaning without a citation.
    import yaml as _yaml
    feeds = [f["name"] for f in _yaml.safe_load(
        (Path(cf.__file__).resolve().parent.parent / "feeds.yaml").read_text(encoding="utf-8")
    )["feeds"]]
    bias = cf.load_bias()

    missing = [n for n in feeds if n not in bias]
    assert not missing, f"feeds with no source_bias.yaml entry at all: {missing}"

    no_funding = [n for n in feeds if not bias[n].get("funding")]
    assert not no_funding, f"feeds with no funding fact: {no_funding}"

    # leaning is a contestable claim -- it must always be cited.
    uncited = [n for n, e in bias.items()
               if e["leaning"] != cf.NOT_RATED["leaning"] and not e.get("cite_url")]
    assert not uncited, f"leaning asserted with no citation: {uncited}"


# ---------- article persistence + extraction (data/articles.jsonl) ----------

def test_article_id_stable_and_falls_back_to_source_title():
    a = _article("PTI", "Some headline", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert cf.article_id(a) == cf.article_id(dict(a))  # same input -> same id
    a["link"] = ""
    b = dict(a)
    assert cf.article_id(a) == cf.article_id(b)  # no link -> source+title fallback, still stable


def _no_extraction(monkeypatch):
    """Most persistence tests aren't testing extraction -- stub it out so
    they never touch the network, and so extract_ok never flips true
    (which would make a second apply_persistence() call skip re-running
    extract_full_text and mask a real regression there)."""
    monkeypatch.setattr(cf, "extract_full_text", lambda url: (None, "stubbed for test"))


def test_apply_persistence_assigns_now_on_first_sighting(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "ARTICLES_FILE", tmp_path / "articles.jsonl")
    _no_extraction(monkeypatch)
    arts = [_article("A", "Brand new story here", datetime(2026, 1, 1, tzinfo=timezone.utc))]
    cf.apply_persistence(arts)
    assert arts[0]["first_seen"] is not None
    stored = cf.load_articles_store()
    assert cf.article_id(arts[0]) in stored


def test_apply_persistence_preserves_timestamp_across_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "ARTICLES_FILE", tmp_path / "articles.jsonl")
    _no_extraction(monkeypatch)
    a = _article("A", "A story that persists", datetime(2026, 1, 1, tzinfo=timezone.utc))
    cf.apply_persistence([a])
    first_ts = a["first_seen"]

    # Same article (by id), a "later run" -- must keep the original timestamp,
    # not overwrite it with whatever "now" is on the second call.
    a2 = _article("A", "A story that persists", datetime(2026, 1, 1, tzinfo=timezone.utc))
    cf.apply_persistence([a2])
    assert a2["first_seen"] == first_ts


def test_apply_persistence_prunes_only_stale_absent_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "ARTICLES_FILE", tmp_path / "articles.jsonl")
    _no_extraction(monkeypatch)
    now = datetime.now(timezone.utc)
    too_old = (now - timedelta(days=cf.ARTICLES_RETENTION_DAYS + 1)).isoformat()
    still_fresh = (now - timedelta(days=1)).isoformat()
    cf.save_articles_store({
        "deadbeef01": {"first_seen": too_old, "raw_text": None, "extract_ok": False, "extract_note": ""},
        "keepme01": {"first_seen": still_fresh, "raw_text": None, "extract_ok": False, "extract_note": ""},
    })

    a = _article("A", "Some other current story", now)
    cf.apply_persistence([a])

    stored = cf.load_articles_store()
    assert "deadbeef01" not in stored     # absent from this run AND past retention -> pruned
    assert "keepme01" in stored           # absent from this run but still within retention -> kept
    assert cf.article_id(a) in stored     # present in this run -> always kept


def test_apply_persistence_tolerates_legacy_first_seen_only_rows(monkeypatch, tmp_path):
    # The file used to be first_seen.jsonl, rows shaped {"id", "first_seen"}
    # with no extraction fields at all -- a real, already-committed shape,
    # not hypothetical. Must load and extend those rows without crashing.
    monkeypatch.setattr(cf, "ARTICLES_FILE", tmp_path / "articles.jsonl")
    _no_extraction(monkeypatch)
    path = tmp_path / "articles.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    aid = "legacyid01"
    path.write_text(json.dumps({"id": aid, "first_seen": now.isoformat()}) + "\n", encoding="utf-8")

    a = _article("A", "Some story", now)
    monkeypatch.setattr(cf, "article_id", lambda x: aid)
    cf.apply_persistence([a])
    assert a["raw_text"] is None   # legacy row had no raw_text -- .get() default, not a crash


def test_load_articles_store_missing_file_is_empty_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "ARTICLES_FILE", tmp_path / "does-not-exist.jsonl")
    assert cf.load_articles_store() == {}


def test_apply_persistence_stores_successful_extraction(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "ARTICLES_FILE", tmp_path / "articles.jsonl")
    monkeypatch.setattr(cf, "extract_full_text", lambda url: ("x" * 1000, ""))
    a = _article("A", "A story worth extracting", datetime(2026, 1, 1, tzinfo=timezone.utc))
    cf.apply_persistence([a])
    assert a["raw_text"] == "x" * 1000
    stored = cf.load_articles_store()[cf.article_id(a)]
    assert stored["extract_ok"] is True


def test_apply_persistence_never_retries_a_successful_extraction(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "ARTICLES_FILE", tmp_path / "articles.jsonl")
    calls = []

    def fake_extract(url):
        calls.append(url)
        return "x" * 1000, ""

    monkeypatch.setattr(cf, "extract_full_text", fake_extract)
    a = _article("A", "A story worth extracting", datetime(2026, 1, 1, tzinfo=timezone.utc))
    cf.apply_persistence([a])
    a2 = _article("A", "A story worth extracting", datetime(2026, 1, 1, tzinfo=timezone.utc))
    cf.apply_persistence([a2])
    assert len(calls) == 1   # second run's extract_ok was already True -- no second fetch
    assert a2["raw_text"] == "x" * 1000


def test_extraction_budget_spread_across_sources_not_feed_order(monkeypatch, tmp_path):
    # The real bug this guards: articles arrive in feeds.yaml order, so a
    # linear scan spent the whole budget on the first-listed feeds. Measured
    # before the fix: 95 of 100 extracted articles came from 3 of 78 feeds.
    # A prolific first source must not be able to starve every later source.
    monkeypatch.setattr(cf, "ARTICLES_FILE", tmp_path / "articles.jsonl")
    monkeypatch.setattr(cf, "EXTRACT_BUDGET_PER_RUN", 6)
    monkeypatch.setattr(cf, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    monkeypatch.setattr(cf, "extract_full_text", lambda url: ("x" * 1000, ""))

    now = datetime.now(timezone.utc)
    # Source "First" is listed first and has far more articles than the rest.
    arts = [_article("First", f"first story {i}", now) for i in range(50)]
    for s in ["Second", "Third", "Fourth"]:
        arts.append(_article(s, f"{s} story", now))

    cf.apply_persistence(arts)
    got = {a["source"] for a in arts if a["raw_text"]}
    # With a budget of 6 and round-robin, every source should get one before
    # "First" gets a second -- so all four appear, not just "First".
    assert got == {"First", "Second", "Third", "Fourth"}, got


def test_apply_persistence_respects_extraction_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "ARTICLES_FILE", tmp_path / "articles.jsonl")
    monkeypatch.setattr(cf, "EXTRACT_BUDGET_PER_RUN", 2)
    calls = []

    def fake_extract(url):
        calls.append(url)
        return "x" * 1000, ""

    monkeypatch.setattr(cf, "extract_full_text", fake_extract)
    now = datetime.now(timezone.utc)
    arts = [_article(f"S{i}", f"story {i}", now) for i in range(5)]
    cf.apply_persistence(arts)
    assert len(calls) == 2   # budget of 2, not one attempt per article
    extracted = [a for a in arts if a["raw_text"]]
    assert len(extracted) == 2


def test_apply_persistence_records_failed_extraction_note(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "ARTICLES_FILE", tmp_path / "articles.jsonl")
    monkeypatch.setattr(cf, "extract_full_text", lambda url: (None, "HTTP 404"))
    a = _article("A", "A story that fails to extract", datetime(2026, 1, 1, tzinfo=timezone.utc))
    cf.apply_persistence([a])
    assert a["raw_text"] is None
    stored = cf.load_articles_store()[cf.article_id(a)]
    assert stored["extract_ok"] is False
    assert stored["extract_note"] == "HTTP 404"


class _FakeResp:
    """Mimics the streaming interface extract_full_text actually uses --
    it reads via iter_content() under a MAX_BYTES cap rather than
    .content, so a fake without iter_content/close would pass a test the
    real code path would fail on."""

    def __init__(self, status_code=200, body=b"", chunk=65536):
        self.status_code = status_code
        self._body = body
        self._chunk = chunk
        self.closed = False

    def iter_content(self, size):
        for i in range(0, len(self._body), self._chunk):
            yield self._body[i:i + self._chunk]

    def close(self):
        self.closed = True


def test_extract_full_text_rejects_short_result(monkeypatch):
    monkeypatch.setattr(cf.requests, "get", lambda *a, **k: _FakeResp(200, b"<html>tiny</html>"))
    monkeypatch.setattr(cf.trafilatura, "extract", lambda *a, **k: "too short")
    text, note = cf.extract_full_text("https://example.com/story")
    assert text is None
    assert "short" in note or "empty" in note


def test_extract_full_text_rejects_non_200(monkeypatch):
    monkeypatch.setattr(cf.requests, "get", lambda *a, **k: _FakeResp(404, b""))
    text, note = cf.extract_full_text("https://example.com/story")
    assert text is None
    assert "404" in note


def test_extract_full_text_refuses_non_http_schemes():
    # url comes from a third-party feed's <link>; whatever is fetched gets
    # published, so anything that isn't plain http(s) must never be requested.
    for bad in ["file:///C:/Windows/win.ini", "ftp://example.com/x", "//evil.com/x", "", None]:
        text, note = cf.extract_full_text(bad)
        assert text is None
        assert note == "refused non-http(s) URL", bad


def test_extract_full_text_caps_body_at_max_bytes(monkeypatch):
    # A <link> pointing at something huge must not be buffered whole.
    oversized = b"x" * (cf.MAX_BYTES + 5 * 65536)
    seen = {}

    def fake_extract(body, url=None):
        seen["len"] = len(body)
        return "y" * 1000

    monkeypatch.setattr(cf.requests, "get", lambda *a, **k: _FakeResp(200, oversized))
    monkeypatch.setattr(cf.trafilatura, "extract", fake_extract)
    text, note = cf.extract_full_text("https://example.com/huge")
    assert text is not None
    # Stops at the first chunk that crosses the cap, so it may overshoot by
    # up to one chunk -- but must not have read the whole oversized body.
    assert seen["len"] < len(oversized)
    assert seen["len"] <= cf.MAX_BYTES + 65536


def test_extract_full_text_handles_request_exception(monkeypatch):
    def raise_it(*a, **k):
        raise cf.requests.exceptions.Timeout("too slow")

    monkeypatch.setattr(cf.requests, "get", raise_it)
    text, note = cf.extract_full_text("https://example.com/story")
    assert text is None
    assert note == "Timeout"
