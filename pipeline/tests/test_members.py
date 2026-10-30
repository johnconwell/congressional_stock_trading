import json
import os
import time
from pathlib import Path

import pytest
import requests

import members
from members import Directory, enrich, load_legislators, norm_name

FIX = Path(__file__).parent / "fixtures"


class FakeGithub:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        if self.fail:
            raise requests.ConnectionError("offline")
        name = url.rsplit("/", 1)[-1]

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return json.loads((FIX / name).read_text(encoding="utf-8"))

        return R()


@pytest.fixture
def directory():
    return Directory(members.download_legislators(FakeGithub()))


def trade(**over):
    t = {"id": "x-1", "chamber": "senate", "member": "Thomas H Tuberville", "memberId": None, "party": None,
         "state": None, "district": None, "gender": None, "birthDate": None, "txDate": "2025-02-03"}
    t.update(over)
    return t


def test_norm_name():
    assert norm_name("Luján, Jr.") == "lujan"
    assert norm_name("Hyde-Smith") == "hyde smith"
    assert norm_name("A. Mitchell") == "a mitchell"
    assert norm_name(None) == ""


def test_download_keeps_only_recent_members_and_needed_fields():
    people = members.download_legislators(FakeGithub())
    assert len(people) == 8
    sinema = next(p for p in people if p["id"] == "S001191")
    assert set(sinema) == {"id", "name", "bio", "terms"}
    assert all(t["end"] >= members.KEEP_SINCE for t in sinema["terms"])
    assert sinema["terms"][-1]["parties"][1]["party"] == "Independent"


def test_load_uses_cache_then_refreshes(tmp_path):
    path = tmp_path / "legislators.json"
    gh = FakeGithub()
    assert len(load_legislators(path, session=gh)) == 8
    assert len(gh.calls) == 2
    load_legislators(path, session=gh)
    assert len(gh.calls) == 2, "fresh cache is reused"
    old = time.time() - 8 * 86400
    os.utime(path, (old, old))
    load_legislators(path, session=gh)
    assert len(gh.calls) == 4, "stale cache is refreshed"


def test_load_falls_back_to_stale_cache_or_none(tmp_path):
    path = tmp_path / "legislators.json"
    assert load_legislators(path, session=FakeGithub(fail=True)) is None
    load_legislators(path, session=FakeGithub())
    assert len(load_legislators(path, session=FakeGithub(fail=True), refresh=True)) == 8


@pytest.mark.parametrize("first,last,office,on,expected", [
    ("Thomas H", "Tuberville", "Tuberville, Tommy (Senator)", "2025-02-03", "T000278"),
    ("Rick", "Scott", "Scott, Rick (Senator)", "2025-03-20", "S001217"),
    ("Timothy E", "Scott", "Scott, Tim (Senator)", "2022-01-01", "S001184"),
    ("Shelley M", "Capito", "Capito, Shelley Moore (Senator)", "2023-01-01", "C001047"),
    ("Markwayne", "Mullin", "Mullin, Markwayne (Senator)", "2024-03-01", "M001190"),
    ("Thomas H", "Tuberville", "Tuberville, Tommy (Senator)", "2015-01-01", None),  # not yet a senator
    ("Nobody", "Unknown", "", "2025-01-01", None),
])
def test_resolve_senator(directory, first, last, office, on, expected):
    p = directory.resolve_senator(first, last, office, on)
    assert (p["id"] if p else None) == expected


def test_two_scotts_without_a_matching_first_name_stay_unresolved(directory):
    assert directory.resolve_senator("X", "Scott", "", "2022-01-01") is None


def test_enrich_senate_trade_from_efd_name(directory):
    t = trade(id="s-1")
    extras = {"s-1": {"efdFirst": "Thomas H", "efdLast": "Tuberville", "office": "Tuberville, Tommy (Senator)"}}
    report = enrich([t], directory, extras=extras)
    assert report.resolved == 1 and not report.unresolved
    assert t == {**trade(id="s-1"), "memberId": "T000278", "member": "Tommy Tuberville", "party": "R",
                 "state": "AL", "district": None, "gender": "M", "birthDate": "1954-09-18"}


def test_enrich_uses_party_on_the_trade_date(directory):
    before = trade(id="a", member="Kyrsten Sinema", memberId="S001191", txDate="2022-06-01")
    after = trade(id="b", member="Kyrsten Sinema", memberId="S001191", txDate="2023-06-01")
    house = trade(id="c", chamber="house", member="Kyrsten Sinema", memberId="S001191", txDate="2018-06-01")
    enrich([before, after, house], directory)
    assert (before["party"], after["party"], house["party"]) == ("D", "I", "D")
    assert house["district"] == 9 and before["district"] is None


def test_enrich_house_trade_keeps_source_values_when_unknown(directory):
    t = trade(chamber="house", member="Someone New", memberId="Z999999", party="R", state="TX")
    report = enrich([t], directory)
    assert t["party"] == "R" and t["state"] == "TX"
    assert report.to_meta()["unresolved"] == {"house: Someone New": 1}


def test_enrich_moves_house_member_to_senate_term(directory):
    house = trade(id="h", chamber="house", member="Markwayne Mullin", memberId="M001190", txDate="2021-05-01")
    senate = trade(id="s", member="Markwayne Mullin", memberId="M001190", txDate="2024-05-01")
    enrich([house, senate], directory)
    assert (house["state"], house["district"]) == ("OK", 2)
    assert (senate["state"], senate["district"]) == ("OK", None)
