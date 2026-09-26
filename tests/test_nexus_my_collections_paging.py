"""get_all_my_collections pages past the 50-collection cap, and
get_collection_status uses it so an older draft isn't reported "missing".

The GraphQL layer is stubbed at ``get_my_collections`` — the paging and status
logic above it is what's under test.
"""
from __future__ import annotations

import pytest

from Nexus.nexus_api import MyCollection, NexusAPI, NexusAPIError


def _col(i: int) -> MyCollection:
    return MyCollection(id=i, slug=f"col-{i}", name=f"Col {i}")


class _FakeAPI(NexusAPI):
    def __init__(self, total: int, fail_at_offset: int | None = None):
        self._all = [_col(i) for i in range(total)]
        self._fail_at = fail_at_offset
        self.calls: list[tuple[int, int]] = []

    def get_my_collections(self, count=50, offset=0, raise_errors=False):
        self.calls.append((count, offset))
        if self._fail_at is not None and offset == self._fail_at:
            raise NexusAPIError("boom")
        return self._all[offset:offset + count]


@pytest.mark.parametrize("total,pages", [(0, 1), (49, 1), (50, 2), (51, 2), (120, 3)])
def test_pages_until_short_page(total, pages):
    api = _FakeAPI(total)
    assert len(api.get_all_my_collections()) == total
    assert len(api.calls) == pages


def test_status_finds_collection_beyond_first_page():
    api = _FakeAPI(75)
    assert api.get_collection_status("col-70") == "ok"
    assert api.get_collection_status("", collection_id=64) == "ok"


def test_failed_page_is_unknown_not_missing():
    api = _FakeAPI(75, fail_at_offset=50)
    assert api.get_collection_status("col-70") == "unknown"
