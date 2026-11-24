#!/usr/bin/env bash
# Run the MosaicPathIndex tests under Proton's Wine: once with the DLL listing
# the game folder itself, once with a file list written by Mosaic's
# Utils.path_index (plus files added after it, which the DLL must notice).
#   WINE=/path/to/wine (default: the newest Proton in compatibilitytools.d)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$here/../.."
bash "$here/build.sh" --test >/dev/null
wine="${WINE:-$(ls -d ~/.local/share/Steam/compatibilitytools.d/*/files/bin/wine 2>/dev/null | tail -1)}"
export WINEPREFIX="$here/build/pfx" WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml="
fail=0
setup() {
    rm -rf "$here/build/game"
    mkdir -p "$here/build/game/Data/SKSE/Plugins"
    cp "$here/build/test_path_index.exe" "$here/build/game/"
    cp "$repo/src/Utils/path_index/MosaicPathIndex.dll" "$here/build/game/Data/SKSE/Plugins/"
}
echo "== DLL lists the game folder itself"
setup
"$wine" "$here/build/game/test_path_index.exe" 2>/dev/null | grep -E "FAIL|lookups|passed" || fail=1
grep -q "no file list from Mosaic" "$here/build/game/Data/SKSE/Plugins/MosaicPathIndex.log" || { echo "FAIL expected a self-scan"; fail=1; }

echo "== with Mosaic's file list"
setup
mkdir -p "$here/build/game/Data/Textures"
for i in $(seq 0 4999); do : > "$here/build/game/Data/Plugin $(printf %04d "$i").esp"; done
(cd "$repo/src" && "$repo/.venv/bin/python" -c "
from pathlib import Path; from Utils.path_index import write_file_list
g = Path('$here/build/game'); print('list:', write_file_list(g, g / 'Data/SKSE/Plugins/MosaicPathIndex.idx'))")
sleep 0.02
: > "$here/build/game/Data/Textures/after_list.dds"
mkdir -p "$here/build/game/Data/New After List/deep" && : > "$here/build/game/Data/New After List/deep/x.txt"
MOSAIC_TEST_AFTER_LIST=1 "$wine" "$here/build/game/test_path_index.exe" 2>/dev/null | grep -E "FAIL|lookups|passed|after the list" || fail=1
grep "file list" "$here/build/game/Data/SKSE/Plugins/MosaicPathIndex.log" || { echo "FAIL the list wasn't used"; fail=1; }
exit $fail
