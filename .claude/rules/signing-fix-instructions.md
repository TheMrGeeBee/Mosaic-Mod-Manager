# Fixing Signing: Git Tags + Flatpak Repo

Two separate signing problems, two separate fixes. Do not conflate them.

---

## 1. Git tag signing ("Unverified" tag on GitHub)

**Root cause:** commit signing was configured previously (SSH-based), but tag
signing is a separate git setting, and every tag created so far was a plain
lightweight tag (`git tag vX.Y.Z`, no flag). Lightweight tags can never carry
a signature, regardless of config — this is why past tags show unverified.

**Fix:**

```bash
git config --global tag.gpgsign true
```

`gpg.format` and `user.signingkey` are already set globally to the existing
SSH key — no new key needed for this part, just this one missing setting.

**Going forward, always create tags as signed, never bare:**

```bash
git tag -s v1.0.0 -m "Release 1.0.0"
git push origin v1.0.0
```

Never use plain `git tag vX.Y.Z` again — it silently produces an
unsignable lightweight tag.

**Verify a tag actually signed correctly:**

```bash
git tag -v v1.0.0
```

**Note:** existing already-pushed tags cannot be retroactively signed.
If any historical tag needs to show as Verified, it must be deleted and
recreated with `-s` — flag this to the user before doing so, don't do it
silently, since deleting a tag has the same downstream effects covered in
this project's git history (orphaned releases, etc.) that have come up before.

---

## 2. Flatpak repo signing ("untrusted" warning on install)

**Root cause:** unrelated to git entirely. The `build.yml` workflow has an
"Import GPG signing key" step that has been silently skipping (no secret
configured) since the first Flatpak build. An unsigned OSTree repo is
exactly what causes Flatpak to warn the file/repo is untrusted.

**Before doing anything: check what the current Flatpak build tooling
actually expects.** Two paths exist:

- **Traditional GPG signing** — what the existing (currently-skipping)
  workflow step is written for.
- **`ostree sign` with an ed25519 keypair** — newer, no GPG dependency,
  simpler key management (no passphrase-in-a-vault problem).

Check the flatpak-builder / ostree version in use in CI and confirm which
signing method it actually supports before generating a key. Report back
which is viable before proceeding — don't assume.

**If generating a new GPG key for this:**
- Do NOT reuse the git-signing SSH key — this needs to be its own key,
  scoped to signing the Flatpak repo only.
- Generate the key, then **immediately save the passphrase somewhere real**
  (a password manager) before doing anything else with it. A GPG key was
  already lost once this week due to an unsaved passphrase — do not repeat
  that. Confirm with the user that the passphrase is actually saved before
  moving on to the next step, don't just assume it's handled.
- Export the private key and add it as a repo secret
  (`Settings → Secrets and variables → Actions`), matching whatever
  variable name the existing "Import GPG signing key" step already expects.

**If using `ostree sign` (ed25519) instead:**
- Generate the ed25519 keypair.
- Store the private key as a repo secret the same way.
- The public key needs to be published/available wherever users add the
  Flatpak remote, so they can verify against it — confirm how this
  project's Flatpak is actually distributed (single `.flatpak` bundle vs.
  a hosted remote) before assuming the usual "user adds our remote" flow
  applies, since right now it's a standalone bundle file, not a hosted repo.

**After wiring in whichever key type:** trigger a real Flatpak build and
confirm the "Import GPG signing key" (or equivalent) step actually runs
and succeeds, rather than continuing to silently skip.
