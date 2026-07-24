# Amethyst Mod Manager — Fork (BG3 modding focus)

## Project context
- Independent fork of ChrisDKN/Amethyst-Mod-Manager (GPL-3). As of 2026-07-24 this is no longer maintained as an upstream-tracking fork: the last PR upstream was closed with no maintainer interest, and the project is going independent — no more PRs upstream, no `upstream` git remote, no upstream syncing
- Focus: Baldur's Gate 3 modding support
- A new project name and new graphics/branding are planned but not yet decided — do not assume or invent a name; ask first, and check GitHub/AUR/Nexus availability before adopting one
- GPL-3 obligations remain regardless of divergence: keep original copyright/license notices intact; an ATTRIBUTION.md crediting ChrisDKN's original project is planned but not yet created
- See `.claude/rules/consolidation-instructions.md` for the independence-transition checklist (naming, ATTRIBUTION.md, hardcoded upstream references still to audit) and file-consolidation conventions

## Version history note
- `sync-upstream.sh` and the `upstream` git remote were removed 2026-07-24 — historical only, no longer applicable. The old rule "fork's version always wins over upstream on conflict" no longer applies since there's nothing to conflict with.

## CI/CD
- Three workflows: `test-build.yml`, `build.yml`, `release.yml`
- Release pipeline triggers on `v*` tags
- Changelog extraction looks for `- v{major.minor.patch}` headers — keep that format exact

## Known-good recovery paths
- If a broad automated tool (e.g. `ruff --fix`) or `git restore .` clobbers uncommitted work, VSCodium Local History has recovered it before — check there first, don't panic-recreate

## Conventions
- Follow existing patterns in the codebase before introducing new ones
- Prefer clean, maintainable code over clever solutions
- Recently touched for code quality: `ue5_game.py`, `nexus_requirements.py`, `ba2_writer.py`

## Gotchas
- Collection install metadata (uploader, category, endorsement) is populated via a background GraphQL fetch during install, reconciled in a Step 5 pass — don't assume it's synchronous
