#!/usr/bin/env bash
# Build MosaicPathIndex.dll (an SKSE plugin, see path_index.c) with MinGW-w64
# into src/Utils/path_index/, where Mosaic installs it from on deploy.
#   Arch:   pacman -S mingw-w64-gcc        Debian/Ubuntu: apt install gcc-mingw-w64-x86-64
# --test also builds test_path_index.exe next to the DLL (run it under Wine,
# see the comment at the top of test_path_index.c).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$here/../../src/Utils/path_index"
cc="${CC:-x86_64-w64-mingw32-gcc}"
flags=(-std=c11 -O2 -Wall -Wextra -Werror -static-libgcc)
"$cc" "${flags[@]}" -shared -o "$out/MosaicPathIndex.dll" "$here/path_index.c"
x86_64-w64-mingw32-strip "$out/MosaicPathIndex.dll" 2>/dev/null || true
echo "built $out/MosaicPathIndex.dll"
if [[ "${1:-}" == "--test" ]]; then
    "$cc" "${flags[@]}" -o "$here/build/test_path_index.exe" "$here/test_path_index.c"
    echo "built $here/build/test_path_index.exe"
fi
