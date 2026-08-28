# FAQ

## What is Mosaic Mod Manager?

A Mo2-style mod manager for Linux, focused on making mod management for
Baldur's Gate 3, Bethesda titles, and dozens of other games work well
natively on Linux — no manual Wine/Proton fiddling required for most
workflows. See the [Key Features](README.md#key-features) section of the
README for the full list.

## Is this related to Amethyst Mod Manager?

Yes. Mosaic Mod Manager started as a fork of
[ChrisDKN/Amethyst-Mod-Manager](https://github.com/ChrisDKN/Amethyst-Mod-Manager),
licensed under GPL-3.0. Full credit and license provenance are documented
in [ATTRIBUTION.md](ATTRIBUTION.md).

## Why the rename?

On 2026-07-24, the project's last pull request to the upstream Amethyst
Mod Manager repo was closed without maintainer interest. Rather than
continue submitting changes upstream with no path to being merged, the
project went independent — no more upstream PRs, no upstream git remote,
no syncing. It relaunched under its own name, Mosaic Mod Manager, on
2026-07-29, to reflect that it's now a separate project with its own
direction rather than a fork tracking someone else's codebase.

## Is it still open source? Same license?

Yes. Mosaic Mod Manager remains GPL-3.0, unchanged and unmodified from
the original. All original copyright and license notices are kept intact.
Anyone is free to use, study, modify, and redistribute it under the same
terms as the original project.

## If I already use Amethyst Mod Manager, what happens when I switch?

Your existing setup carries over automatically. On first launch, Mosaic
migrates your config from `~/.config/AmethystModManager` to
`~/.config/MosaicModManager`, so existing profiles and mod lists are
preserved. New game setups default their mod staging folder to
`~/Games/Mosaic` instead of `~/Games/Amethyst`, but this only affects
games you add fresh — anything already staged under the old path keeps
working from there.

## Why does the version say v1.0.4 when Amethyst was already past v2.0?

Version numbering reset to `v1.0.0` on 2026-07-29 to mark the independent
relaunch. Nothing under the old Amethyst numbering ever shipped past what
became this project's `v1.0.0`, so no release history was actually lost —
just renumbered to give the independent project a clean starting point.

## What's new since the fork?

A condensed list of highlights since `v1.0.0` — see
[Changelog.txt](Changelog.txt) for the complete, unabridged history:

- Per-mod reorder locking, so locked mods can't be accidentally dragged,
  sorted, or removed
- mod.io update handling for BG3 mods brought to parity with the existing
  Nexus update flow (version picker, ignore-update support)
- A true single-instance lock, so opening a Nexus download link never
  spawns a second, conflicting window
- Per-mod custom URLs, for attaching a wiki page, Discord thread, or
  patch-notes link to any mod
- Shared custom tools that work across every game instead of needing to
  be re-added per game
- Native Linux execution for custom tools — a tool with no `.exe`/`.bat`
  now runs directly on the host instead of always being forced through
  Proton
- Native Linux BodySlide and Outfit Studio support for Bethesda games,
  with no Proton or Wine prefix needed

## Why do a few things still reference ChrisDKN's GitHub account?

A small number of *content* sources — curated community profiles, some
game handlers and wizard plugins, and translations — are still pulled
from the original project's `Resources` branch, because that's genuinely
where that community content lives today. This is an intentional,
functional dependency, not leftover branding, and will move to Mosaic's
own hosting over time.

## Will Mosaic ever merge changes back from Amethyst, or vice versa?

No. There's no active syncing in either direction — Mosaic Mod Manager is
maintained independently going forward.

## Where do I get support, report a bug, or request a game?

Use [GitHub Issues](https://github.com/TheMrGeeBee/Mosaic-Mod-Manager/issues)
on this repo — templates are available for both bug reports and game
support requests.
