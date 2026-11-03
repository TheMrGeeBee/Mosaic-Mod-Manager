"""Verify a collection manifest's recorded FOMOD choices against the real
``ModuleConfig.xml``, and turn them into the selections ``resolve_files`` replays.

A Nexus collection records a mod's FOMOD install as

    "choices": {"type": "fomod",
                "options": [{"name": <step>,
                             "groups": [{"name": <group>,
                                         "choices": [{"name": <plugin>,
                                                      "idx": <int>}]}]}]}

where ``options[i]`` lines up with ``installSteps[i]``, a group's ``choices``
array holds ONLY the plugins the author selected (an empty array means "the
author deliberately picked nothing here"), and ``idx`` is the plugin's index
WITHIN its group.

Replay matches on the plugin NAME, because that survives the manifest's
``options[]`` being ordered differently from ``installSteps`` (see
``fomod_installer._build_group_fallback``). ``idx`` is only a fallback for when
the name matches nothing — the author's recorded file and the file we just
downloaded can be different builds of the mod, and the two genuinely disagree in
the wild (Starfield's "Jiggle Adjustments All in One" records four picks whose
``idx`` points at a different plugin than the name does), so a name that DOES
match always wins.

Pure: no I/O, no Qt. The caller supplies an already-parsed ``ModuleConfig``.
Used by the installer (to warn when a recorded pick doesn't resolve) and by
``cli.py audit-collection-fomod`` (to check a manifest without reinstalling it).
"""

from __future__ import annotations

from dataclasses import dataclass

# Group types a FOMOD will not let a user leave empty: the wizard forces a pick,
# so a manifest that recorded none for one of these is missing data rather than
# expressing "nothing". Deliberately NOT SelectAny / SelectAtMostOne — an empty
# array is a legitimate, complete answer for those, and defaulting them would
# install files the author left out.
ENFORCED_GROUP_TYPES = ("SelectExactlyOne", "SelectAtLeastOne")

# Findings that mean a pick the author recorded did NOT make it into the install.
WARNING_KINDS = ("plugin-name-miss", "group-not-in-step", "step-count",
                 "step-order", "dup-plugin-name")


@dataclass(frozen=True)
class ChoiceFinding:
    """One discrepancy between the manifest and the FOMOD it describes."""
    kind: str
    detail: str
    step: int = -1
    group: str = ""
    name: str = ""
    idx: "int | None" = None

    @property
    def is_warning(self) -> bool:
        return self.kind in WARNING_KINDS

    def __str__(self) -> str:
        where = ""
        if self.step >= 0:
            where = f"step {self.step}"
            if self.group:
                where += f" group '{self.group}'"
            where = f" ({where})"
        return f"{self.detail}{where}"


def convert_choices(choices: dict) -> "dict[str, dict[str, list[str]]]":
    """Shape-only conversion of a manifest ``choices`` block to the
    ``{str(step_idx): {group: [plugins]}}`` form ``resolve_files`` takes.

    Keeps a group whose ``choices`` array is empty as an explicit ``[]`` — that
    is the author saying "nothing here", and ``_selected_in_group`` is written so
    an explicit empty entry beats the cross-step group-name fallback. Dropping it
    instead let another step's same-named group bleed in.

    Config-free, so it is also what the pre-extract code uses to decide whether
    the author recorded anything at all.
    """
    result: dict[str, dict[str, list[str]]] = {}
    for step_idx, step in enumerate(choices.get("options") or []):
        groups: dict[str, list[str]] = {}
        for group in step.get("groups") or []:
            groups[group.get("name", "")] = [
                c["name"] for c in (group.get("choices") or []) if c.get("name")]
        result[str(step_idx)] = groups
    return result


def resolve_recorded_choices(config, choices: dict
                             ) -> "tuple[dict[str, dict[str, list[str]]], list[ChoiceFinding]]":
    """``(selections, findings)`` for replaying *choices* against *config*.

    *selections* is ready for ``resolve_files``: names are matched against the
    FOMOD's actual plugins, a name that misses falls back to its recorded ``idx``,
    and an explicitly-empty group stays explicitly empty. *findings* describes
    everything that didn't line up — the installer logs them, so a pick that
    could not be applied is never dropped in silence.
    """
    findings: list[ChoiceFinding] = []
    manifest_steps = list(choices.get("options") or [])
    xml_steps = list(getattr(config, "steps", None) or [])

    if manifest_steps and len(manifest_steps) != len(xml_steps):
        findings.append(ChoiceFinding(
            "step-count",
            f"the collection recorded {len(manifest_steps)} install step(s) but "
            f"this FOMOD has {len(xml_steps)} — the mod's file may have changed "
            f"since the collection was built"))

    # How many times each group name occurs across the whole FOMOD: a name used
    # once is safe to report as "not in this step but elsewhere" (resolve_files'
    # group-name fallback can still rescue it); a repeated one cannot.
    group_occurrences: dict[str, int] = {}
    for step in xml_steps:
        for group in step.groups:
            group_occurrences[group.name] = group_occurrences.get(group.name, 0) + 1

    selections: dict[str, dict[str, list[str]]] = {}
    for step_idx, m_step in enumerate(manifest_steps):
        x_step = xml_steps[step_idx] if step_idx < len(xml_steps) else None
        m_name = (m_step.get("name") or "").strip()
        if x_step is not None and m_name and x_step.name.strip() and m_name != x_step.name.strip():
            findings.append(ChoiceFinding(
                "step-order",
                f"the collection's step {step_idx} is '{m_name}' but this FOMOD's "
                f"step {step_idx} is '{x_step.name}' — matching on group name instead",
                step=step_idx))
        x_groups = {g.name: g for g in (x_step.groups if x_step is not None else [])}

        groups: dict[str, list[str]] = {}
        for m_group in m_step.get("groups") or []:
            g_name = m_group.get("name", "")
            recorded = [c for c in (m_group.get("choices") or []) if c.get("name")]
            x_group = x_groups.get(g_name)

            if x_group is None:
                groups[g_name] = [c["name"] for c in recorded]
                if recorded:
                    seen = group_occurrences.get(g_name, 0)
                    findings.append(ChoiceFinding(
                        "group-not-in-step",
                        f"the collection recorded {len(recorded)} pick(s) for group "
                        f"'{g_name}', which this FOMOD's step {step_idx} does not "
                        + ("have (it appears in another step — trying to match it "
                           "there)" if seen else "have at all"),
                        step=step_idx, group=g_name))
                continue

            plugin_names = [p.name for p in x_group.plugins]
            picked: list[str] = []
            for c in recorded:
                name, idx = c["name"], c.get("idx")
                if name in plugin_names:
                    picked.append(name)
                    if plugin_names.count(name) > 1:
                        findings.append(ChoiceFinding(
                            "dup-plugin-name",
                            f"'{name}' appears {plugin_names.count(name)} times in "
                            f"group '{g_name}' — every copy of it will be installed",
                            step=step_idx, group=g_name, name=name, idx=idx))
                elif isinstance(idx, int) and 0 <= idx < len(plugin_names):
                    picked.append(plugin_names[idx])
                    findings.append(ChoiceFinding(
                        "idx-fallback",
                        f"the collection's pick '{name}' is not in group '{g_name}' "
                        f"any more; using the option at its recorded position "
                        f"{idx} instead ('{plugin_names[idx]}')",
                        step=step_idx, group=g_name, name=name, idx=idx))
                else:
                    findings.append(ChoiceFinding(
                        "plugin-name-miss",
                        f"the collection's pick '{name}' is not an option in group "
                        f"'{g_name}' and its recorded position ({idx}) is out of "
                        f"range — this pick was NOT applied",
                        step=step_idx, group=g_name, name=name, idx=idx))
            groups[g_name] = picked

        selections[str(step_idx)] = groups

    findings.extend(_audit_unrecorded_required(config, selections))
    if manifest_steps and not any(any(v.values()) for v in selections.values()):
        findings.append(ChoiceFinding(
            "converts-empty",
            "the collection recorded this mod's FOMOD but selected nothing in any "
            "group — only its always-installed files will be installed"))
    return selections, findings


def _audit_unrecorded_required(config, selections: dict) -> "list[ChoiceFinding]":
    """Findings for groups a FOMOD will not let a user leave empty but the
    manifest recorded nothing for — only in steps that are actually VISIBLE
    given the recorded picks.

    Invisible steps matter: a FOMOD that branches on "which mod manager?" carries
    a full parallel set of steps for the branch nobody took, and the manifest
    correctly records nothing for those. Reporting them would bury the real
    findings (44 of them in Starfield Revisited alone, every one harmless).
    ``resolve_files`` fills the visible ones from the FOMOD's own defaults.
    """
    from Utils.installers.fomod_installer import evaluate_dependency

    findings: list[ChoiceFinding] = []
    flag_state: dict[str, str] = {}
    for i, step in enumerate(getattr(config, "steps", None) or []):
        if step.visible_condition is not None and not evaluate_dependency(
                step.visible_condition, flag_state, set(), None,
                version_pass=True, loose_files=None):
            continue
        step_sel = selections.get(str(i), {})
        for group in step.groups:
            if group.group_type in ENFORCED_GROUP_TYPES and not step_sel.get(group.name):
                findings.append(ChoiceFinding(
                    "required-defaulted",
                    f"the collection recorded no pick for group '{group.name}', "
                    f"which is a {group.group_type} — using this FOMOD's own "
                    f"default instead",
                    step=i, group=group.name))
            chosen = set(step_sel.get(group.name, []))
            for plugin in group.plugins:
                if group.group_type == "SelectAll" or plugin.name in chosen:
                    flag_state.update(plugin.condition_flags)
    return findings


def audit_recorded_choices(config, choices: dict) -> "list[ChoiceFinding]":
    """Findings only — for the CLI auditor, which doesn't install anything."""
    return resolve_recorded_choices(config, choices)[1]


def audit_manifest_shape(choices: dict) -> "list[ChoiceFinding]":
    """The checks that need no ``ModuleConfig``, so a manifest can still be
    audited after its archives have been cleared from the download cache."""
    findings: list[ChoiceFinding] = []
    converted = convert_choices(choices)
    if not any(any(groups.values()) for groups in converted.values()):
        findings.append(ChoiceFinding(
            "converts-empty",
            "the collection recorded this mod's FOMOD but selected nothing in any "
            "group — only its always-installed files will be installed"))
    return findings


# ---------------------------------------------------------------------------
# Whole-manifest audit — "does this collection's FOMOD data survive a replay?"
# ---------------------------------------------------------------------------
@dataclass
class ModAudit:
    """One manifest entry's verdict."""
    name: str
    file_id: int
    has_choices: bool
    has_hashes: bool
    # True = ModuleConfig.xml parsed and the choices were checked against it;
    # False = the archive or its ModuleConfig could not be read; None = archives
    # weren't available, so only the config-free checks ran.
    checked_against_fomod: "bool | None" = None
    findings: "list[ChoiceFinding]" = None  # type: ignore[assignment]
    # Did the install actually apply what the manifest recorded? None when there
    # is no <profile>/fomod/<mod>.json to compare against.
    applied_matches: "bool | None" = None
    note: str = ""

    def __post_init__(self):
        if self.findings is None:
            self.findings = []

    @property
    def warnings(self) -> "list[ChoiceFinding]":
        return [f for f in self.findings if f.is_warning]


def audit_collection_manifest(manifest: dict, *, parse_fomod=None,
                              applied_selections=None) -> "list[ModAudit]":
    """Audit every FOMOD-bearing entry in *manifest*.

    *parse_fomod(mod_entry)* returns a parsed ``ModuleConfig`` for that mod, or
    None when its archive isn't available. Supply it to check the recorded picks
    against the FOMOD's real options; omit it and only the config-free checks run.

    *applied_selections(mod_entry)* returns the selections an install actually
    applied (``<profile>/fomod/<mod>.json``), so a finished profile can be
    compared against the manifest without re-reading a single archive — which is
    the only way to verify a collection whose archives have been cleared.

    Pure apart from the two callbacks; all I/O belongs to the caller.
    """
    results: list[ModAudit] = []
    for mod in manifest.get("mods") or []:
        choices = mod.get("choices") or {}
        hashes = [h for h in (mod.get("hashes") or [])
                  if isinstance(h, dict) and h.get("path") and h.get("md5")]
        if choices.get("type") != "fomod" and not hashes:
            continue
        src = mod.get("source") or {}
        audit = ModAudit(
            name=(src.get("logicalFilename") or mod.get("name") or "?"),
            file_id=int(src.get("fileId") or 0),
            has_choices=choices.get("type") == "fomod",
            has_hashes=bool(hashes),
        )
        if audit.has_choices:
            config = parse_fomod(mod) if parse_fomod is not None else None
            if config is not None:
                audit.checked_against_fomod = True
                selections, audit.findings = resolve_recorded_choices(config, choices)
            else:
                audit.checked_against_fomod = None if parse_fomod is None else False
                selections = convert_choices(choices)
                audit.findings = audit_manifest_shape(choices)
            if applied_selections is not None:
                applied = applied_selections(mod)
                if applied is not None:
                    audit.applied_matches = _selections_equal(selections, applied)
        results.append(audit)
    return results


def _selections_equal(a: dict, b: dict) -> bool:
    """Compare two selection maps ignoring groups that select nothing — an empty
    group and an absent one mean the same thing to ``resolve_files``, and the two
    formats differ on which they write."""
    def norm(d):
        out = {}
        for step, groups in (d or {}).items():
            picked = {g: sorted(v) for g, v in (groups or {}).items() if v}
            if picked:
                out[str(step)] = picked
        return out
    return norm(a) == norm(b)


def format_audit(results: "list[ModAudit]", *, no_choice_names=()) -> str:
    """The human-readable report the CLI prints."""
    lines: list[str] = []
    checked = sum(1 for r in results if r.checked_against_fomod)
    with_choices = sum(1 for r in results if r.has_choices)
    with_hashes = sum(1 for r in results if r.has_hashes)
    warned = [r for r in results if r.warnings]
    infos = [r for r in results if r.findings and not r.warnings]
    compared = [r for r in results if r.applied_matches is not None]
    diverged = [r for r in results if r.applied_matches is False]

    lines.append(f"FOMOD entries in the manifest: {len(results)} "
                 f"({with_choices} with recorded choices, {with_hashes} with a "
                 f"recorded file manifest)")
    lines.append(f"  checked against the real ModuleConfig.xml: {checked}")
    if compared:
        lines.append(f"  compared against what the install applied: {len(compared)}"
                     f" — {len(diverged)} diverged")
    lines.append(f"  entries with a pick that would NOT be applied: {len(warned)}")
    lines.append(f"  entries with a non-blocking note: {len(infos)}")

    for label, rows in (("PROBLEMS", warned), ("NOTES", infos)):
        if not rows:
            continue
        lines.append("")
        lines.append(f"### {label}")
        for r in rows:
            lines.append(f"  {r.name}")
            for f in r.findings:
                lines.append(f"    {'! ' if f.is_warning else '- '}{f}")

    if diverged:
        lines.append("")
        lines.append("### APPLIED != RECORDED")
        for r in diverged:
            lines.append(f"  {r.name}")

    unreadable = [r for r in results if r.checked_against_fomod is False]
    if unreadable:
        lines.append("")
        lines.append(f"### NOT CHECKED ({len(unreadable)} archive(s) unavailable "
                     f"or without a readable ModuleConfig.xml)")
        for r in unreadable[:20]:
            lines.append(f"  {r.name}{(' — ' + r.note) if r.note else ''}")
        if len(unreadable) > 20:
            lines.append(f"  … {len(unreadable) - 20} more")

    if no_choice_names:
        lines.append("")
        lines.append(f"### NO RECORDED CHOICES ({len(no_choice_names)})")
        lines.append("  The collection specifies nothing for these; the installer "
                     "applies the FOMOD's own defaults.")
        for n in sorted(no_choice_names):
            lines.append(f"  • {n}")
    return "\n".join(lines)
