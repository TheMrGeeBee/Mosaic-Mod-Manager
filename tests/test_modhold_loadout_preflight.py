"""The preflight dialog: what is shown, how choices change the plan, profile naming."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from gui_qt.modhold.modhold_loadout_preflight import (
    ModholdLoadoutPreflight,
    default_profile_name,
    free_profile_name,
)
from Modhold.loadout_manifest import parse_loadout


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _file(fid, size):
    return {"id": fid, "name": f"f{fid}.zip", "version": "1.0", "size": size, "sha256": "ab" * 32}


def _item(pos, mod_id, files, **kw):
    return {"position": pos, "optional": kw.get("optional", False), "note": kw.get("note", ""),
            "mod": {"id": mod_id, "name": kw.get("name", f"Mod {mod_id}"), "version": "1",
                    "author": "A", "page": f"https://modholdmods.com/m/{mod_id}"},
            "locked": kw.get("locked"), "files": files, "nexus": None}


def _external(pos, name, **kw):
    return {"position": pos, "optional": kw.get("optional", False), "note": "",
            "mod": {"id": None, "name": name, "page": f"https://n/{name}"}, "locked": None,
            "files": [], "nexus": f"https://n/{name}"}


def _loadout(name="Estates of Skyrim - Sim Settlements", signed_in=True):
    return parse_loadout({
        "format": "modhold-loadout", "version": 1, "id": 3, "name": name, "summary": "Every estate.",
        "by": "Seth", "game": {"slug": "skyrim-se"}, "signed_in": signed_in, "items": [
            _item(1, 10, [_file(100, 2_000_000)]),
            _item(2, 20, [_file(200, 500_000)], optional=True, name="Optional Home", note="big"),
            _item(3, 30, [_file(300, 1_000)], locked="beta"),
            _external(4, "SkyUI"),
            _external(5, "Optional ext", optional=True)]})


def test_default_profile_name_follows_the_nexus_recipe():
    assert default_profile_name(_loadout()) == "Estates_of_Skyrim_-_Sim_Settlements"
    assert default_profile_name(_loadout("!!!")) == "Loadout_3"
    assert len(default_profile_name(_loadout("x" * 200))) == 64


def test_free_profile_name_counts_up():
    assert free_profile_name("A", set()) == "A"
    assert free_profile_name("A", {"A"}) == "A_2"
    assert free_profile_name("A", {"A", "A_2", "A_3"}) == "A_4"


def test_summary_reports_hosted_external_and_beta(qapp):
    dlg = ModholdLoadoutPreflight(_loadout(), set())
    text = dlg._summary.text()
    assert "3 mod(s) from Modhold" in text and "3 file(s)" in text
    assert "2 item(s) are hosted elsewhere" in text
    assert "1 item(s) are beta releases" in text
    assert "left out" not in text


def test_optional_items_start_ticked_and_untick_to_leave_them_out(qapp):
    dlg = ModholdLoadoutPreflight(_loadout(), set())
    assert len(dlg._optional) == 2 and all(b.isChecked() for b, _ in dlg._optional)
    assert len(dlg.plan().installable) == 3 and len(dlg.plan().external) == 2
    hosted_box = next(b for b, _ in dlg._optional if "Optional Home" in b.text())
    hosted_box.setChecked(False)
    plan = dlg.plan()
    assert [i.mod_id for i in plan.installable] == [10, 30]
    assert "1 optional item(s) left out" in dlg._summary.text()
    assert "2 mod(s) from Modhold" in dlg._summary.text()
    ext_box = next(b for b, _ in dlg._optional if "Optional ext" in b.text())
    ext_box.setChecked(False)
    assert len(dlg.plan().external) == 1 and "2 optional item(s) left out" in dlg._summary.text()


def test_an_unhosted_optional_item_says_so_in_its_label(qapp):
    dlg = ModholdLoadoutPreflight(_loadout(), set())
    labels = [b.text() for b, _ in dlg._optional]
    assert any("Optional ext" in t and "hosted elsewhere" in t for t in labels)
    assert any(t == "Optional Home" for t in labels)
    assert next(b for b, _ in dlg._optional if "Optional Home" in b.text()).toolTip() == "big"


def test_a_new_profile_gets_the_default_name_and_the_name_can_be_edited(qapp):
    dlg = ModholdLoadoutPreflight(_loadout(), {"Other"})
    assert not dlg.continue_existing()
    assert dlg.profile_name() == "Estates_of_Skyrim_-_Sim_Settlements"
    dlg._name_edit.setText("My Estates Test!")
    assert dlg.profile_name() == "My_Estates_Test"
    dlg._name_edit.setText("Other")                       # taken: counted up, never overwritten
    assert dlg.profile_name() == "Other_2"
    dlg._name_edit.setText("   ")                         # blank falls back to the default
    assert dlg.profile_name() == "Estates_of_Skyrim_-_Sim_Settlements"


def test_an_existing_profile_offers_continue_and_it_is_the_default(qapp):
    base = "Estates_of_Skyrim_-_Sim_Settlements"
    dlg = ModholdLoadoutPreflight(_loadout(), {base})
    assert dlg._name_edit is None and dlg.continue_existing()
    assert dlg.profile_name() == base
    # choosing the other radio creates a fresh, never-colliding profile
    dlg._new_radio.setChecked(True)               # exclusive: this unchecks "continue"
    assert not dlg.continue_existing() and dlg.profile_name() == f"{base}_2"


def test_not_signed_in_tells_the_user_to_connect(qapp):
    assert "Connect your Modhold account" in ModholdLoadoutPreflight(
        _loadout(signed_in=False), set())._summary.text()
    assert "Connect your Modhold account" not in ModholdLoadoutPreflight(
        _loadout(signed_in=True), set())._summary.text()


def test_a_loadout_without_optional_items_has_no_optional_list(qapp):
    lo = parse_loadout({"format": "modhold-loadout", "version": 1, "id": 9, "name": "Small",
                        "by": "A", "game": {"slug": "skyrim-se"}, "items": [_item(1, 1, [_file(1, 10)])]})
    dlg = ModholdLoadoutPreflight(lo, set())
    assert dlg._optional == [] and len(dlg.plan().installable) == 1


def test_accept_and_reject_give_the_codes_the_app_checks(qapp):
    from PySide6.QtWidgets import QDialog
    ok = ModholdLoadoutPreflight(_loadout(), set())
    ok.accept()
    assert ok.result() == QDialog.DialogCode.Accepted
    no = ModholdLoadoutPreflight(_loadout(), set())
    no.reject()
    assert no.result() != QDialog.DialogCode.Accepted


# ------------------------------------------------------------------ Nexus section

def _nexus_item(pos, name, mod_id, optional=False):
    url = f"https://www.nexusmods.com/skyrimspecialedition/mods/{mod_id}"
    return {"position": pos, "optional": optional, "note": "",
            "mod": {"id": None, "name": name, "page": url}, "locked": None, "files": [], "nexus": url}


def _nexus_loadout():
    return parse_loadout({
        "format": "modhold-loadout", "version": 1, "id": 3, "name": "Estates", "by": "S",
        "game": {"slug": "skyrim-se"}, "signed_in": True, "items": [
            _item(1, 10, [_file(100, 1_000)]),
            _nexus_item(2, "SkyUI", 12604),
            _nexus_item(3, "SKSE", 30379),
            _nexus_item(4, "Hidden Mod", 99),
            _nexus_item(5, "Address Library", 32444, optional=True),
            _external(6, "High Poly Head")]})


def _picks(lo, ambiguous=()):
    from types import SimpleNamespace

    from Modhold.nexus_links import NexusPick
    files = {12604: ("SkyUI-5.7z", "5.2"), 30379: ("SKSE-2-3-1.7z", "2.3.1"),
             32444: ("AddrLib.7z", "13")}
    out = {}
    for it in lo.items:
        if it.nexus_ref is None:
            continue
        spec = files.get(it.nexus_ref.mod_id)
        f = SimpleNamespace(file_id=it.nexus_ref.mod_id, file_name=spec[0], version=spec[1],
                            name="Main") if spec else None
        out[it.order_index] = NexusPick(it.order_index, it.name, it.nexus_ref, f,
                                        ambiguous=it.name in ambiguous,
                                        reason="" if f else "it has no main file")
    return out


def test_nexus_items_are_listed_with_the_chosen_file_and_ticked(qapp):
    from PySide6.QtWidgets import QLabel
    lo = _nexus_loadout()
    dlg = ModholdLoadoutPreflight(lo, set(), nexus_picks=_picks(lo, ambiguous={"SKSE"}))
    texts = [b.text() for b, _ in dlg._nexus]
    assert "SkyUI → SkyUI-5.7z (5.2)" in texts
    assert any(t.startswith("SKSE → SKSE-2-3-1.7z (2.3.1)") and "newest of several" in t for t in texts)
    assert all(b.isChecked() for b, _ in dlg._nexus)
    # a mod with nothing to pick is shown with its reason, not as a tickable download
    labels = [lbl.text() for lbl in dlg.findChildren(QLabel)]
    assert any("Hidden Mod" in t and "no main file" in t for t in labels)
    assert not any("Hidden Mod" in b.text() for b, _ in dlg._nexus)


def test_ticked_nexus_items_join_the_plan_and_leave_the_manual_list(qapp):
    lo = _nexus_loadout()
    dlg = ModholdLoadoutPreflight(lo, set(), nexus_picks=_picks(lo))
    plan = dlg.plan()
    assert [i.name for i in plan.nexus] == ["SkyUI", "SKSE", "Address Library"]
    assert [i.name for i in plan.external] == ["Hidden Mod", "High Poly Head"]
    assert "3 item(s) will be downloaded from Nexus" in dlg._summary.text()
    assert "2 item(s) are hosted elsewhere" in dlg._summary.text()


def test_unticking_a_nexus_item_moves_it_back_to_the_manual_list(qapp):
    lo = _nexus_loadout()
    dlg = ModholdLoadoutPreflight(lo, set(), nexus_picks=_picks(lo))
    next(b for b, _ in dlg._nexus if b.text().startswith("SkyUI")).setChecked(False)
    plan = dlg.plan()
    assert [i.name for i in plan.nexus] == ["SKSE", "Address Library"]
    assert "SkyUI" in [i.name for i in plan.external]
    assert "2 item(s) will be downloaded from Nexus" in dlg._summary.text()


def test_an_unticked_optional_item_leaves_even_if_its_nexus_box_is_ticked(qapp):
    lo = _nexus_loadout()
    dlg = ModholdLoadoutPreflight(lo, set(), nexus_picks=_picks(lo))
    next(b for b, _ in dlg._optional if "Address Library" in b.text()).setChecked(False)
    plan = dlg.plan()
    assert "Address Library" not in [i.name for i in plan.nexus]
    assert [i.name for i in plan.skipped_optional] == ["Address Library"]


def test_without_nexus_the_note_explains_why_and_the_items_stay_manual(qapp):
    lo = _nexus_loadout()
    dlg = ModholdLoadoutPreflight(lo, set(), nexus_picks=None,
                                  nexus_note="Nexus Premium is needed to download the Nexus items.")
    assert dlg._nexus == []
    assert "Nexus Premium is needed" in dlg._summary.text()
    plan = dlg.plan()
    assert plan.nexus == [] and len(plan.external) == 5
    assert dlg.nexus_picks() == {}


def test_the_note_is_not_shown_for_a_loadout_with_no_nexus_links(qapp):
    dlg = ModholdLoadoutPreflight(_loadout(), set(), nexus_picks=None, nexus_note="NOTE")
    assert "NOTE" not in dlg._summary.text()


def test_nexus_picks_are_handed_back_for_the_installer(qapp):
    lo = _nexus_loadout()
    picks = _picks(lo)
    assert ModholdLoadoutPreflight(lo, set(), nexus_picks=picks).nexus_picks() == picks
