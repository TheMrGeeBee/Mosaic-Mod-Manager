---
paths:
  - ".github/workflows/*.yml"
  - "Changelog.txt"
---

# Release & CI/CD conventions

- Three workflows: `test-build.yml`, `build.yml`, `release.yml`. Don't consolidate them without checking why they were split.
- `release.yml` triggers on `v*` tags.
- Changelog extraction parses `- v{major.minor.patch}` headers exactly — any deviation (extra text on the header line, different bullet/heading style) breaks the release notes extraction. Verify a new header matches this format before committing.
- **Dispatching `release.yml` manually (`workflow_dispatch`, e.g. `publish_nexus: true` from `main`) does NOT create a GitHub Release or publish the Flatpak GitHub Pages remote.** The `release` and `publish-pages` jobs are both gated on `github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')`, so a branch dispatch skips them — quietly (shows as `skipped` in `gh run view`, not a failure) rather than erroring, which makes it easy to miss. Hit this exact gap shipping v1.0.4: the Nexus upload went out fine via dispatch, but the app's own auto-updater (`Utils/version_check.py`, reads `GET /repos/.../releases/latest`) kept reporting the previous version since no tag or Release ever existed. Fix: push a real signed tag on the exact commit that was already built/tested — `git tag -s vX.Y.Z <commit> -m "Release X.Y.Z"` then `git push origin vX.Y.Z` (see the `fix-signing` skill for the signing convention). This is safe to do after an already-completed Nexus publish — it won't re-trigger `publish-nexus` (that job is dispatch-only gated, never satisfied by a plain tag `push` event), so no duplicate-upload risk.
