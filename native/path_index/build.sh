#!/usr/bin/env bash
# Build MosaicPathIndex (a script-extender plugin, see path_index.c) with
# MinGW-w64 into src/Utils/path_index/, where Mosaic installs it from on deploy:
#   MosaicPathIndex.dll    64-bit: SKSE64, SKSE VR, F4SE, F4SE VR, SFSE
#   MosaicPathIndex32.dll  32-bit: SKSE (Skyrim LE, Enderal), NVSE, FOSE, OBSE
# (each is installed into the game as MosaicPathIndex.dll).
#   Arch:   pacman -S mingw-w64-gcc
#   Debian/Ubuntu: apt install gcc-mingw-w64-x86-64 gcc-mingw-w64-i686
# --test also builds test_path_index.exe and test_path_index32.exe (run them
# under Wine, see the comment at the top of test_path_index.c).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$here/../../src/Utils/path_index"
flags=(-std=c11 -O2 -Wall -Wextra -Werror -static-libgcc)
build() {   # <triplet> <dll name> <test exe name>
    local cc="$1-gcc"
    "$cc" "${flags[@]}" -shared -o "$out/$2" "$here/path_index.c"
    "$1-strip" "$out/$2" 2>/dev/null || true
    echo "built $out/$2"
    if [[ "${TEST:-}" == 1 ]]; then
        mkdir -p "$here/build"
        "$cc" "${flags[@]}" -o "$here/build/$3" "$here/test_path_index.c"
        echo "built $here/build/$3"
    fi
}
[[ "${1:-}" == "--test" ]] && TEST=1
build x86_64-w64-mingw32 MosaicPathIndex.dll test_path_index.exe
build i686-w64-mingw32 MosaicPathIndex32.dll test_path_index32.exe
