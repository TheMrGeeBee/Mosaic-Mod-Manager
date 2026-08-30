# Signing the AppImage

Separate task from the git-tag and Flatpak signing fixes (see
signing-fix-instructions.md) — different mechanism, different key,
lower urgency. Do not start this until the Flatpak signing fix has been
verified working (test-build.yml green, GPGKey= present, etc.).

---

## Context

Unlike Flatpak, there's no runtime "untrusted" warning for an unsigned
AppImage — nothing blocks or nags the user today. This is a defense-in-depth
improvement, not a bug fix:

- Lets a user (or a tool like AppImageUpdate) verify a downloaded AppImage
  hasn't been tampered with or corrupted, independent of trusting GitHub's
  HTTPS delivery alone.
- `appimagetool` supports embedding a GPG signature at build time via
  `--sign` (and a matching `--sign-key=<KEYID>` / similar, check current
  `appimagetool --help` for exact current flag names/behavior before
  assuming syntax from memory).
- Complements the existing `.zsync` sidecar files already published for
  delta updates — signature verification and update-checking working
  together, not overlapping.

## Key requirements

- **A brand-new dedicated key.** Do not reuse:
  - The existing personal GPG key (Gordon Bergström <mrgeebee@pm.me>,
    CF314A8F794D12163E922C3D3DF3C68D60A9AB03)
  - The Flatpak signing key (Mosaic Mod Manager <mrgeebee@pm.me>,
    DFE556C37CBFA2DBB7548F0F508C6624C03400AC)
  - The SSH key used for commit signing
- **No passphrase**, same reasoning as the Flatpak key: none of the CI
  workflows have pinentry-loopback/passphrase handling wired up, so an
  unattended import+sign would hang waiting on a prompt. The security
  boundary is the GitHub encrypted secret + this key never being reused
  elsewhere, not a passphrase.
- ed25519, matching the style of the other two keys already in use for
  this project, unless `appimagetool`'s signing step has some specific
  compatibility requirement that rules that out — check first.

## Steps

1. Confirm exactly what `appimagetool` (the actual version pinned/used in
   this project's `make-appimage.sh` / CI container) expects for signing —
   flag name, whether it wants a detached `.sig` file alongside the
   AppImage or an embedded signature, and whether AppImageUpdate/Gear
   Lever/AppImageLauncher actually verify it automatically or whether a
   user has to do so manually. Report back before generating anything —
   don't assume the mechanism matches Flatpak's.
2. Generate the dedicated key (batch mode, no passphrase, matching the
   pattern already used for the Flatpak key).
3. **Hard pause point — same as the Flatpak key.** Do not proceed until
   the user has:
   - Copied the exported private key `.asc` (and public key `.asc`) into
     KeePassXC as a new, clearly-named entry (e.g. "Mosaic AppImage GPG
     Signing Key — private key backup"), including the fingerprint in
     the entry notes.
   - Explicitly confirmed here, after actually checking it's there —
     not just acknowledging the request — that both files are saved.
   Remind the user, same as last time: GitHub Actions secrets are
   write-only, so this local backup plus the keyring on this machine are
   the only recovery path if this key is ever needed again.
4. Wire the key into `make-appimage.sh` / the relevant CI workflow step,
   gated the same defensive way the Flatpak signing is (only sign if the
   secret is actually present, never hard-fail a build if it's momentarily
   missing — match the existing `HAVE_GPG` pattern rather than inventing
   a new one).
5. Verify via `test-build.yml` (or equivalent dry-run), confirming the
   signing step actually executes and produces the expected signature
   artifact, before this ever touches a real release.

## Explicitly out of scope

- Retroactively signing any already-published AppImage release.
- Changing anything about the Flatpak or git-tag signing setup — those
  are done and verified separately.
