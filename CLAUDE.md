# Mosaic Mod Manager (BG3 modding focus)

## Project context
- Independent project, renamed from Amethyst Mod Manager on 2026-07-29. Began as a fork of ChrisDKN/Amethyst-Mod-Manager (GPL-3) — see `ATTRIBUTION.md`. No longer an upstream-tracking fork as of 2026-07-24: the last PR upstream was closed with no maintainer interest, so the project went independent — no more PRs upstream, no `upstream` git remote, no upstream syncing
- Focus: Baldur's Gate 3 modding support
- GPL-3 obligations remain regardless of divergence: original copyright/license notices are kept intact; `ATTRIBUTION.md` credits ChrisDKN's original project
- The GitHub repo is `TheMrGeeBee/Mosaic-Mod-Manager`; the flatpak app-id is `io.github.TheMrGeeBee.MosaicModManager`; the config directory is `~/.config/MosaicModManager` (auto-migrated from `~/.config/AmethystModManager` on first launch, see `Utils/config_paths.py`)
- `Utils/gh_sync.py` and `Utils/profile/curated_profiles.py` intentionally still pull from `ChrisDKN/Amethyst-Mod-Manager`'s `Resources` branch — real, currently-only-there content (custom handlers, wizard plugins, translations, curated profiles). Not a stale reference; don't "fix" it without setting up Mosaic's own Resources branch first
- The AUR package and the self-hosted Flatpak update remote don't exist yet — `Utils/version_check.py`'s `_HOSTED_INFRA_AVAILABLE` flag gates every function that depends on them (disabled, not deleted). Flip it once that infra is actually published
- See `.claude/rules/consolidation-instructions.md` for the original independence-transition checklist (naming, ATTRIBUTION.md, hardcoded upstream references) and file-consolidation conventions — naming is now resolved (Mosaic Mod Manager), the rest is done as of the 2026-07-29 rename pass

## Version history note
- `sync-upstream.sh` and the `upstream` git remote were removed 2026-07-24 — historical only, no longer applicable. The old rule "fork's version always wins over upstream on conflict" no longer applies since there's nothing to conflict with.
- Version numbering reset to `v1.0.0-beta.1` on 2026-07-29 (was `v2.0.6-beta.9`) to mark the independent relaunch under the new name. Same scheme as before: SemVer + `-beta.N` pre-release suffix, accumulating changelog entries under one unreleased version header until a plain `vX.Y.Z` tag cuts the stable release.

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
