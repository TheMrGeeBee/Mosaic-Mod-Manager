"""is_mod_removed: a deleted Nexus mod must not trigger the browser fallback."""
from types import SimpleNamespace

from Nexus.manual_download_watch import is_mod_removed
from Nexus.nexus_api import NexusAPIError
from Nexus.nexus_download import DownloadResult


def _res(msg, code=404):
    return DownloadResult(success=False, error=msg, status_code=code)


class _Api:
    def __init__(self, exc=None):
        self.exc = exc

    def get_mod(self, game, mod_id):
        if self.exc:
            raise self.exc
        return SimpleNamespace(mod_id=mod_id)


def test_no_mod_found_is_removed():
    assert is_mod_removed(_res("No Mod Found: 588"))


def test_missing_file_with_live_mod_is_archived():
    assert not is_mod_removed(_res("No File found for mod: 181"), _Api(), "g", 181)


def test_missing_file_with_404_mod_is_removed():
    api = _Api(NexusAPIError("No Mod Found: 181", status_code=404))
    assert is_mod_removed(_res("No File found for mod: 181"), api, "g", 181)


def test_lookup_failure_keeps_fallback():
    api = _Api(NexusAPIError("boom", status_code=500))
    assert not is_mod_removed(_res("No File found for mod: 181"), api, "g", 181)


def test_non_404_and_success_are_not_removed():
    assert not is_mod_removed(_res("No Mod Found", code=403))
    assert not is_mod_removed(DownloadResult(success=True))


def test_cross_domain_entry_resolves_to_manifest_domain():
    """Epic Edition rev 70 pins a 'site' mod and a Spyro mod; they must be
    requested under their own domain, not the collection's game."""
    from types import SimpleNamespace as NS
    from Utils.collections.collection_install import _manual_domain
    idx = NS(file_id_to_domain={9633: "site", 397: "spyroreignitedtrilogy"})
    game = "taintedgrailthefallofavalon"
    assert _manual_domain(idx, game, NS(file_id=9633, domain_name="")) == "site"
    assert _manual_domain(
        idx, game, NS(file_id=397, domain_name="")) == "spyroreignitedtrilogy"
    assert _manual_domain(idx, game, NS(file_id=1, domain_name="")) == game
