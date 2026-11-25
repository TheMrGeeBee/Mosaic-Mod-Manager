#!/usr/bin/env bash
# Run the MosaicPathIndex tests under Proton's Wine, for the 64-bit and the
# 32-bit build, each loaded the way a script extender loads it (its plugin
# folder, its <X>SEPlugin_Load): once with the DLL listing the game folder
# itself, once with a file list written by Mosaic's Utils.path_index (plus
# files added after it, which the DLL must notice).
#   WINE=/path/to/wine (default: the newest Proton in compatibilitytools.d)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$here/../.."
bash "$here/build.sh" --test >/dev/null
wine="${WINE:-$(ls -d ~/.local/share/Steam/compatibilitytools.d/*/files/bin/wine 2>/dev/null | tail -1)}"
export WINEPREFIX="$here/build/pfx" WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml="
fail=0
game="$here/build/game"

setup() {   # <bits> <extender>
    rm -rf "$game"
    mkdir -p "$game/Data/$2/Plugins"
    if [[ $1 == 32 ]]; then
        cp "$here/build/test_path_index32.exe" "$game/test.exe"
        cp "$repo/src/Utils/path_index/MosaicPathIndex32.dll" "$game/Data/$2/Plugins/MosaicPathIndex.dll"
    else
        cp "$here/build/test_path_index.exe" "$game/test.exe"
        cp "$repo/src/Utils/path_index/MosaicPathIndex.dll" "$game/Data/$2/Plugins/"
    fi
}

self_scan() {   # <bits> <extender>
    echo "== $1-bit as a $2 plugin: DLL lists the game folder itself"
    setup "$1" "$2"
    MOSAIC_TEST_XSE="$2" "$wine" "$game/test.exe" 2>/dev/null \
        | grep -E "FAIL|lookups|passed|loaded as" || fail=1
    grep -q "no file list from Mosaic" "$game/Data/$2/Plugins/MosaicPathIndex.log" \
        || { echo "FAIL expected a self-scan logged next to the DLL"; fail=1; }
    grep -q "($1-bit)" "$game/Data/$2/Plugins/MosaicPathIndex.log" \
        || { echo "FAIL the log doesn't say $1-bit"; fail=1; }
}

with_list() {   # <bits> <extender>
    echo "== $1-bit as a $2 plugin: with Mosaic's file list"
    setup "$1" "$2"
    mkdir -p "$game/Data/Textures"
    for i in $(seq 0 4999); do : > "$game/Data/Plugin $(printf %04d "$i").esp"; done
    (cd "$repo/src" && "$repo/.venv/bin/python" -c "
from pathlib import Path; from Utils.path_index import write_file_list
g = Path('$game'); print('list:', write_file_list(g, g / 'Data/$2/Plugins/MosaicPathIndex.idx'))")
    sleep 0.02
    : > "$game/Data/Textures/after_list.dds"
    mkdir -p "$game/Data/New After List/deep" && : > "$game/Data/New After List/deep/x.txt"
    MOSAIC_TEST_XSE="$2" MOSAIC_TEST_AFTER_LIST=1 "$wine" "$game/test.exe" 2>/dev/null \
        | grep -E "FAIL|lookups|passed|after the list" || fail=1
    grep "file list" "$game/Data/$2/Plugins/MosaicPathIndex.log" \
        || { echo "FAIL the list wasn't used"; fail=1; }
}

self_scan 64 SKSE
with_list 64 SKSE
self_scan 64 F4SE
self_scan 64 SFSE
self_scan 32 NVSE
with_list 32 NVSE
self_scan 32 SKSE
self_scan 32 FOSE
self_scan 32 OBSE
exit $fail
