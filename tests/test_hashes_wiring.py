"""Guards the wiring that carries a collection author's `hashes` to the
installer.

These are structural checks rather than behavioural ones because the bug they
exist to catch was structural: `install_collection_archive` was called from two
places in the collection installer, and only one of them passed
``fomod_auto_hashes``. The resolver worked perfectly; it was simply never
reached on the deferred path, so a cross-mod-gated FOMOD opened the wizard even
though the author had recorded the exact file set (observed with
'Particle Patch for ENB', 438 hashes).
"""
from __future__ import annotations

import ast
import inspect

import Utils.collections.collection_install as ci
import Utils.mods.mod_install as mi


def _calls_to(tree, func_name):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", None)) == func_name]


def test_every_installer_call_that_passes_choices_also_passes_hashes():
    """The two are alternative recordings of the same decision, so any call
    site offering one must offer the other."""
    tree = ast.parse(inspect.getsource(ci))
    calls = _calls_to(tree, "install_collection_archive")
    assert calls, "no install_collection_archive call sites found"
    offenders = []
    for c in calls:
        kw = {k.arg for k in c.keywords}
        if "fomod_auto_selections" in kw and "fomod_auto_hashes" not in kw:
            offenders.append(c.lineno)
    assert not offenders, (
        f"install_collection_archive called with fomod_auto_selections but not "
        f"fomod_auto_hashes at line(s) {offenders} — a hashes-only FOMOD "
        f"reaching that path would fall through to the wizard")


def test_both_the_inline_and_deferred_paths_are_covered():
    """Regression guard: the deferred path is the one that was missed."""
    tree = ast.parse(inspect.getsource(ci))
    calls = _calls_to(tree, "install_collection_archive")
    with_hashes = [c for c in calls
                   if "fomod_auto_hashes" in {k.arg for k in c.keywords}]
    assert len(with_hashes) >= 2, (
        "expected at least two call sites passing fomod_auto_hashes "
        "(the inline install and the deferred-FOMOD pass)")


def test_process_deferred_accepts_the_hashes_map():
    sig = inspect.signature(ci._process_deferred)
    assert "file_id_to_hashes" in sig.parameters
    p = sig.parameters["file_id_to_hashes"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY, (
        "keyword-only so the existing positional arguments stay undisturbed")
    assert p.default is None


def test_installer_exposes_the_hashes_parameter_as_keyword_only():
    sig = inspect.signature(mi.install_collection_archive)
    assert "fomod_auto_hashes" in sig.parameters
    p = sig.parameters["fomod_auto_hashes"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is None


def test_both_defer_guards_consult_the_hashes():
    """The early (pre-extract) and post-extract guards must both know about
    hashes, or the mod is deferred before the resolver can ever run."""
    src = inspect.getsource(mi.install_collection_archive)
    assert src.count("fomod_auto_hashes") >= 3, (
        "expected fomod_auto_hashes in both defer guards and the resolve branch")
