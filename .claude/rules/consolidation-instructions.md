# File Consolidation & Project Independence — Instructions for Claude Code

## Context
This project (~550+ .py files, 70+ subfolders) started as a fork of
ChrisDKN/Amethyst-Mod-Manager. The goal now is twofold:
1. Consolidate the file structure into something more navigable.
2. Evaluate/prepare for this becoming an independent project rather than
   an active fork (still deciding — treat as likely, not certain).

Do not do this as one giant commit. Work in small, reviewable, reversible
steps. Ask before any step that changes behavior, not just structure.

---

## 1. Before starting anything

- Tag the current state: `git tag pre-consolidation-<date>`
- Confirm the working tree is clean and everything is pushed to origin
  before making any structural changes.
- Do this work on its own dedicated branch — never mixed into a branch
  intended for an upstream PR, and never mixed into ordinary bugfix work.

## 2. How to move files

- Use `git mv` explicitly for pure relocations. Never do a delete +
  recreate — that breaks git's rename detection and makes
  `git log --follow` / `git blame -C` useless for tracing history later.
- **Separate "move" commits from "change" commits.** A commit that moves
  a file must not also alter its logic. If a move reveals something that
  should also be fixed/changed, do that as its own follow-up commit.
- Batch by subsystem, not all at once — e.g. one commit/PR-sized batch for
  `Games/`, another for `Nexus/`, another for `gui_qt/`, etc. Each batch
  should be independently reviewable and independently revertable.
- Write real commit messages for each move: what moved, where, and why
  (e.g. "Merge nexus_requirements.py and nexus_update_checker.py into
  nexus_metadata.py — both did overlapping per-mod Nexus lookups").
  Never just "refactor" or "reorganize files."

## 3. Verification after every batch

- Run the actual import/smoke check after each subsystem batch, not just
  at the end of the whole pass:
  `python3 -c "import gui_qt.app"` (or whatever the real entry-point
  check is) — catch a broken import immediately, not 40 commits later.
- If there's an existing test suite or smoke-test script, run it after
  every batch too.
- Flag anything where consolidating two files together requires a genuine
  decision (e.g. two functions with the same name but different
  behavior) — don't silently pick one; ask.

## 4. Scope discipline

- This pass is about *file organization*, not fixing bugs or changing
  behavior. If something looks wrong while moving it, note it separately
  rather than fixing it inline — keep the consolidation diff clean and
  behavior-neutral so any regression is obviously from the reorg, not
  hidden among unrelated fixes.

---

## 5. If this becomes an independent project (not just a fork)

Flag this section as "check with me before acting" — these are bigger,
more permanent decisions than file moves.

- **GPL-3 obligations remain** regardless of how much the code changes or
  diverges. Keep the original copyright/license notices intact, and add
  a short `NOTICE.md` or `ATTRIBUTION.md` crediting the original
  ChrisDKN/Amethyst-Mod-Manager project as the base this was forked from.
- **Audit for hardcoded references to the original repo** and flag every
  one found, rather than silently changing or leaving them:
  - README links and badges
  - `.desktop` file metadata
  - In-app "check for updates" / GitHub link targets
  - `PKGBUILD` `url=` field
  - Issue/PR templates
  - CI workflow comments referencing the original repo
- **`sync-upstream.sh` and the `upstream` git remote** — if no longer
  merging from ChrisDKN's branches, flag this rather than silently
  deleting it. Options: remove it, or mark it clearly as historical/inert
  in a comment, in case there's ever a reason to selectively pull a fix
  from upstream again.
- **Naming**: do not assume a new name — ask before adopting one, and
  check it isn't already in use on GitHub/AUR/Nexus if a name is chosen,
  to avoid a painful rename later once users/packages exist.
