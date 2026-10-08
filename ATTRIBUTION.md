# Attribution

Mosaic Mod Manager began as a fork of [Amethyst Mod
Manager](https://github.com/ChrisDKN/Amethyst-Mod-Manager) by ChrisDKN,
originally distributed under the GNU General Public License v3.0 (see
`LICENSE`).

As of 2026-07-24 this project is no longer maintained as an
upstream-tracking fork — it has diverged into an independent project with
its own name, branding, and direction, focused specifically on Baldur's
Gate 3 modding. No code was copied in from any other source after that
point; all changes since are original work building on the forked
codebase.

The GPL-3.0 license carries forward unmodified: this project remains free
software, and anyone is free to use, study, modify, and redistribute it
under the same terms as the original.

Thank you to ChrisDKN and everyone who contributed to the original Amethyst Mod Manager for the foundation this project was built on.

## Third-party data

- **Fallout 4 1.10.163 Steam depot manifests**
  (`src/Utils/steam/versions/377160.json`) come from
  [zerratar/fallout4-downgrader](https://github.com/zerratar/fallout4-downgrader)
  (FO4Down, GPL-3.0), `src/FO4Down/DepotManager.cs` at commit
  `9123b12ed8d1`. Only the depot→manifest id table is used, as data. The
  Game version (Steam) wizard's code is original. One entry (Nuka-World
  Russian, depot 393899) is left out because FO4Down lists it with the same
  manifest as Nuka-World French, which is almost certainly a copy error.
