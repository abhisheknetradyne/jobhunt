"""Parsers + prefilter, run against the fixtures in their native ATS shapes.

No network, no API key. This is the suite that catches the two bugs that cost
me an evening each: Lever's epoch-milliseconds timestamps, and a bare `sde`
regex that silently matches nothing.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobhunt import mock
from jobhunt.fetch import parse_ashby, parse_greenhouse, parse_lever, strip_html
from jobhunt.mock import fetch_all_mock
from jobhunt.prefilter import prefilter

CONFIG = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml")
                        .read_text(encoding="utf-8"))
FILTERS = CONFIG["filters"]


# ------------------------------------------------------------- strip_html ---

def test_strip_html_unescapes_twice():
    """Greenhouse ships HTML-entity-escaped HTML: unescape, strip, unescape."""
    raw = "&lt;p&gt;Go &amp;amp; Java&lt;/p&gt;"
    assert strip_html(raw) == "Go & Java"


def test_strip_html_turns_block_tags_into_newlines():
    out = strip_html("<p>One</p><p>Two</p><ul><li>a</li><li>b</li></ul>")
    assert "One" in out and "Two" in out and "a" in out and "b" in out
    assert "<" not in out


def test_strip_html_handles_none_and_empty():
    assert strip_html(None) == ""
    assert strip_html("") == ""


# ---------------------------------------------------------------- parsers ---

def test_greenhouse_maps_every_field():
    jobs = parse_greenhouse("acme-edge", "Acme Edge", mock.GREENHOUSE["acme-edge"])
    j = next(j for j in jobs if j.title.startswith("Software Engineer II"))
    assert j.job_id == "greenhouse:acme-edge:5501001"
    assert j.ats == "greenhouse"
    assert j.company == "Acme Edge"
    assert j.location == "Bangalore, India"
    assert j.url.startswith("https://boards.greenhouse.io/")
    assert "distributed services" in j.description


def test_lever_concatenates_description_lists_and_additional():
    """The requirements live in lists[], not descriptionPlain. Drop the
    concatenation and every Lever job looks unqualified."""
    jobs = parse_lever("quantstack", "QuantStack", mock.LEVER["quantstack"])
    j = next(j for j in jobs if j.title == "Backend Engineer (Go)")
    assert "market data pipeline" in j.description      # descriptionPlain
    assert "Requirements" in j.description              # lists[].text
    assert "2-5 years backend experience" in j.description  # lists[].content
    assert "No take-home" in j.description              # additionalPlain


def test_lever_createdAt_is_epoch_milliseconds():
    """1.7e12 is milliseconds. Reading it as seconds dates the post to 1970
    and the freshness filter eats the whole board without a word."""
    today = datetime.now(timezone.utc).date()
    jobs = parse_lever("quantstack", "QuantStack", mock.LEVER["quantstack"])
    j = next(j for j in jobs if j.title == "Backend Engineer (Go)")
    assert j.posted_at == today.isoformat()


def test_ashby_skips_unlisted_drafts():
    jobs = parse_ashby("helioscale", "Helioscale", mock.ASHBY["helioscale"])
    assert all("unlisted" not in j.url for j in jobs)
    assert len(jobs) == 2   # 3 postings, one isListed: false


def test_ashby_reads_compensation_and_html_fallback():
    jobs = parse_ashby("helioscale", "Helioscale", mock.ASHBY["helioscale"])
    networking = next(j for j in jobs if j.title == "Software Engineer, Networking")
    assert networking.salary == "₹32L – ₹48L"
    ds = next(j for j in jobs if j.title == "Data Scientist, Growth")
    assert "Causal inference" in ds.description   # descriptionHtml fallback


def test_job_ids_are_globally_unique_and_namespaced():
    jobs = fetch_all_mock()
    ids = [j.job_id for j in jobs]
    assert len(ids) == len(set(ids))
    assert all(re.match(r"^(greenhouse|lever|ashby):[^:]+:.+$", i) for i in ids)


def test_parsers_take_decoded_json_not_a_response():
    """Parsers are pure: body in, list[Job] out. That is what makes --mock
    exercise the real code path instead of a second implementation."""
    assert parse_greenhouse("x", "X", {}) == []
    assert parse_lever("x", "X", []) == []
    assert parse_ashby("x", "X", {}) == []


# -------------------------------------------------------------- prefilter ---

def test_exclude_only_mode_has_no_include_patterns():
    assert FILTERS.get("include_titles") in ([], None)


def test_bare_sde_regex_does_not_match_the_spelled_out_title():
    """Reminder: bare `sde` does not match "Software Development Engineer".
    With exclude-only filters this no longer matters for the gate, but the
    regex quirk is still worth pinning if anyone re-adds includes."""
    assert not re.search(r"\bsde\b", "Software Development Engineer", re.I)
    assert re.search(r"\bsde\b", "SDE II", re.I)


@pytest.mark.parametrize("title", [
    "Staff Software Engineer, Storage",       # too senior
    "Principal Engineer",                     # too senior
    "Engineering Manager, Platform",          # management track
    "Enterprise Account Executive",           # wrong function
    "Frontend Engineer, Design Systems",      # frontend
    "Mobile App Developer",                   # mobile
    "iOS Engineer",                           # mobile
    "QA Engineer",                            # QA
    "SDET, Platform",                         # QA
    "Software Test Engineer",                 # QA
    "Data Scientist, Growth",                 # wrong discipline
])
def test_junk_titles_are_rejected(title):
    exc = FILTERS["exclude_titles"]
    assert any(re.search(p, title, re.I) for p in exc), f"{title!r} not excluded"


@pytest.mark.parametrize("title", [
    "Software Engineer II, Distributed Systems",
    "Software Development Engineer, Core Infra",
    "Backend Engineer (Go)",
    "Site Reliability Engineer",
    "Senior Software Engineer, Platform",   # "senior" is allowed; resume is Senior SWE
    "Full Stack Engineer",                  # not in exclude list — LLM can reject
])
def test_swe_titles_are_not_excluded(title):
    exc = FILTERS["exclude_titles"]
    assert not any(re.search(p, title, re.I) for p in exc), title


def test_full_mock_funnel_keeps_only_the_five_real_matches():
    kept, stats = prefilter(fetch_all_mock(), FILTERS)
    titles = sorted(j.title for j in kept)
    assert titles == [
        "Backend Engineer (Go)",
        "Site Reliability Engineer",
        "Software Development Engineer, Core Infra",
        "Software Engineer II, Distributed Systems",
        "Software Engineer, Networking",
    ]
    assert stats["kept"] == 5
    assert stats["input"] == len(fetch_all_mock())


def test_stale_posting_is_dropped_by_freshness_gate():
    """Planted Senior+stale fixture is dropped by age (senior is no longer
    title-excluded). Age is also tested with an explicit mid-level stale job."""
    from jobhunt.fetch import Job
    from jobhunt.mock import STALE_DAYS, _ago

    kept_mock, _ = prefilter(fetch_all_mock(), FILTERS)
    assert not any("Senior Software Engineer, Platform" == j.title for j in kept_mock)

    stale = Job(
        job_id="gh:x:stale", ats="greenhouse", company="X",
        title="Backend Engineer", location="Bangalore, India",
        url="https://example.com", description="Go",
        posted_at=_ago(STALE_DAYS).isoformat(),
    )
    fresh = Job(
        job_id="gh:x:fresh", ats="greenhouse", company="X",
        title="Backend Engineer", location="Bangalore, India",
        url="https://example.com", description="Go",
        posted_at=_ago(0).isoformat(),
    )
    kept, stats = prefilter([stale, fresh], FILTERS)
    assert [j.job_id for j in kept] == ["gh:x:fresh"]
    assert stats["age"] == 1


def test_wrong_city_dropped_but_remote_kept():
    kept, _stats = prefilter(fetch_all_mock(), FILTERS)
    assert not any("San Francisco" in (j.location or "") for j in kept)
    assert any("Remote" in (j.location or "") for j in kept)


def test_allow_remote_is_what_lets_an_out_of_region_remote_role_through():
    """"Remote (India)" already matches the `india` location, so it is the
    wrong fixture for this. Use a remote role that names no allowed city."""
    from jobhunt.fetch import Job
    remote = Job(job_id="lever:x:1", ats="lever", company="X",
                 title="Backend Engineer", location="Remote - Global",
                 url="https://example.com", description="Go")

    kept_on, _ = prefilter([remote], dict(FILTERS, allow_remote=True))
    kept_off, _ = prefilter([remote], dict(FILTERS, allow_remote=False))

    assert len(kept_on) == 1
    assert kept_off == []


def test_empty_filters_keep_everything():
    jobs = fetch_all_mock()
    kept, stats = prefilter(jobs, {})
    assert len(kept) == len(jobs)
    assert stats["kept"] == len(jobs)


def test_max_age_days_override_widens_the_window():
    from jobhunt.fetch import Job
    from jobhunt.mock import STALE_DAYS, _ago

    stale = Job(
        job_id="gh:x:stale", ats="greenhouse", company="X",
        title="Backend Engineer", location="Bangalore, India",
        url="https://example.com", description="Go",
        posted_at=_ago(STALE_DAYS).isoformat(),
    )
    _kept_default, stats_default = prefilter([stale], FILTERS)
    kept_off, stats_off = prefilter([stale], dict(FILTERS, max_age_days=None))
    assert stats_default["age"] == 1
    assert stats_off["age"] == 0
    assert stats_off["max_age_days"] is None
    assert len(kept_off) == 1


def test_empty_digest_includes_funnel_diagnostics():
    from jobhunt import digest as digest_mod

    funnel = {
        "scanned": 100,
        "title": 80,
        "location": 10,
        "age": 5,
        "max_age_days": 30,
        "passed_filters": 5,
        "already_seen": 5,
        "candidates": 0,
        "in_digest": 0,
    }
    subject, doc = digest_mod.build([], 100, 0, {"tracked": 5, "applied": 0}, funnel=funnel)
    assert "scanned 100" in subject
    assert "Dropped title=80" in doc
    assert "Already seen: 5" in doc
    assert "max_age_days=30" in doc
