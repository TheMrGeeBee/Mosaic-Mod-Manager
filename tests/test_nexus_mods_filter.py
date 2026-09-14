"""NexusAPI._build_mods_filter: the shared GraphQL ModsFilter builder used by
every Nexus browser search call (search_mods, get_top_mods, trending, ...).

Extended to support a language filter (the in-app browser's new "Languages"
sidebar, mirroring Nexus's own site) alongside the existing category filter —
each group OR-combined among itself, all AND-ed together with the domain
filter. The exact shapes here were validated against the live GraphQL API
(languageName is a real filter field, confirmed by querying it directly)
before this was wired into the UI.
"""
from __future__ import annotations

from Nexus.nexus_api import NexusAPI


def test_domain_only():
    f = NexusAPI._build_mods_filter("baldursgate3")
    assert f == {"gameDomainName": {"value": "baldursgate3"}}


def test_single_category():
    f = NexusAPI._build_mods_filter("baldursgate3", ["Gameplay"])
    assert f == {
        "op": "AND",
        "filter": [
            {"gameDomainName": {"value": "baldursgate3"}},
            {"categoryName": {"value": "Gameplay"}},
        ],
    }


def test_multiple_categories_or_combined():
    f = NexusAPI._build_mods_filter("baldursgate3", ["Gameplay", "Visual"])
    assert f == {
        "op": "AND",
        "filter": [
            {"gameDomainName": {"value": "baldursgate3"}},
            {"op": "OR", "filter": [
                {"categoryName": {"value": "Gameplay"}},
                {"categoryName": {"value": "Visual"}},
            ]},
        ],
    }


def test_single_language_only():
    f = NexusAPI._build_mods_filter("baldursgate3", language_names=["German"])
    assert f == {
        "op": "AND",
        "filter": [
            {"gameDomainName": {"value": "baldursgate3"}},
            {"languageName": {"value": "German"}},
        ],
    }


def test_multiple_languages_or_combined():
    f = NexusAPI._build_mods_filter(
        "baldursgate3", language_names=["German", "French"])
    assert f == {
        "op": "AND",
        "filter": [
            {"gameDomainName": {"value": "baldursgate3"}},
            {"op": "OR", "filter": [
                {"languageName": {"value": "German"}},
                {"languageName": {"value": "French"}},
            ]},
        ],
    }


def test_categories_and_languages_combined():
    f = NexusAPI._build_mods_filter(
        "baldursgate3", category_names=["Gameplay"], language_names=["German"])
    assert f == {
        "op": "AND",
        "filter": [
            {"gameDomainName": {"value": "baldursgate3"}},
            {"categoryName": {"value": "Gameplay"}},
            {"languageName": {"value": "German"}},
        ],
    }


def test_multiple_categories_and_multiple_languages():
    f = NexusAPI._build_mods_filter(
        "baldursgate3", category_names=["Gameplay", "Visual"],
        language_names=["German", "French"])
    assert f == {
        "op": "AND",
        "filter": [
            {"gameDomainName": {"value": "baldursgate3"}},
            {"op": "OR", "filter": [
                {"categoryName": {"value": "Gameplay"}},
                {"categoryName": {"value": "Visual"}},
            ]},
            {"op": "OR", "filter": [
                {"languageName": {"value": "German"}},
                {"languageName": {"value": "French"}},
            ]},
        ],
    }


def test_empty_lists_behave_like_none():
    assert (NexusAPI._build_mods_filter("baldursgate3", [], [])
           == NexusAPI._build_mods_filter("baldursgate3"))
