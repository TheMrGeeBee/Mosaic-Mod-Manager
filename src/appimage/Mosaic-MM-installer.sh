#!/bin/bash
# Mosaic Mod Manager installer
# Downloads latest AppImage, icon, and creates a .desktop entry
#
# Portable across Linux distros: uses XDG paths (~/.local/share) and
# creates ~/Applications if missing. Requires curl or wget.

set -e

ALLOW_PRERELEASE=0
for arg in "$@"; do
    case "$arg" in
        --prerelease) ALLOW_PRERELEASE=1 ;;
    esac
done

REPO="TheMrGeeBee/Mosaic-Mod-Manager"
BASE_URL="https://raw.githubusercontent.com/${REPO}/main"
ICON_URL="${BASE_URL}/src/icons/title-bar.png"
RELEASES_API_URL="https://api.github.com/repos/${REPO}/releases/latest"
RELEASES_LIST_API_URL="https://api.github.com/repos/${REPO}/releases?per_page=20"

# ~/Applications: not standard on all distros; we create it (common on Steam Deck)
APPLICATIONS_DIR="${HOME}/Applications"
# XDG Base Dir: standard on all desktop Linux (Ubuntu, Fedora, Arch, etc.)
XDG_DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
ICONS_DIR="${XDG_DATA}/icons"
APPLICATIONS_DESKTOP_DIR="${XDG_DATA}/applications"

# Local name is fixed so .desktop entry and updates overwrite the same file
APPIMAGE_NAME="MosaicModManager-x86_64.AppImage"
ICON_NAME="title-bar.png"
DESKTOP_NAME="mosaic-mod-manager.desktop"

echo "Mosaic Mod Manager installer"
echo "=============================="

# Discover latest AppImage from GitHub Releases
echo "Checking for latest version..."
if [ "$ALLOW_PRERELEASE" = "1" ]; then
    URL="$RELEASES_LIST_API_URL"
else
    URL="$RELEASES_API_URL"
fi
# Honor GITHUB_TOKEN (or GH_TOKEN) to lift the 60 req/hour unauthenticated
# rate limit — useful on shared IPs and in CI.
GH_AUTH_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
if command -v curl &>/dev/null; then
    if [ -n "$GH_AUTH_TOKEN" ]; then
        JSON="$(curl -sL -H "Authorization: Bearer $GH_AUTH_TOKEN" "$URL")"
    else
        JSON="$(curl -sL "$URL")"
    fi
else
    if [ -n "$GH_AUTH_TOKEN" ]; then
        JSON="$(wget -qO- --header="Authorization: Bearer $GH_AUTH_TOKEN" "$URL")"
    else
        JSON="$(wget -qO- "$URL")"
    fi
fi

# Distinguish a GitHub API error (rate limit, network, private repo) from a
# real missing asset — otherwise the check below blames a missing AppImage.
if echo "$JSON" | grep -q '"message" *: *"API rate limit exceeded'; then
    echo "Error: GitHub API rate limit exceeded for your IP." >&2
    echo "Wait ~1 hour, or set GITHUB_TOKEN=<your token> and retry." >&2
    exit 1
fi

# Both paths parse with the same grep/sed pipeline. On /releases/latest the
# response is a single object; on /releases?per_page=N it's an array sorted
# newest-first by published_at — so head -1 picks the newest release in both
# cases. We deliberately avoid python3 here: the installer is invoked by the
# running AppImage, whose env can leave the bundled python's sys.path pointing
# at a now-unmounted FUSE path, causing import failures.
LATEST_VERSION="$(echo "$JSON" | grep -o '"tag_name" *: *"[^"]*"' | sed 's/.*: *"v\{0,1\}\([^"]*\)"/\1/' | head -1)"
APPIMAGE_URL="$(echo "$JSON" | grep -o '"browser_download_url" *: *"[^"]*\.AppImage"' | sed 's/.*: *"\([^"]*\)"/\1/' | head -1)"
if [ -z "$APPIMAGE_URL" ]; then
    echo "Error: Could not find an AppImage asset in the latest release." >&2
    exit 1
fi
echo "Latest version: ${LATEST_VERSION}"
echo ""

# TARGET_APPIMAGE (optional): set by an in-app self-update to the path of the
# AppImage file actually being run, when that differs from our own default
# ~/Applications location. That happens whenever a third-party integration
# tool (Gear Lever, AppImageLauncher, ...) has moved/renamed the file it
# manages — writing to the default path regardless would silently create a
# second, orphaned copy while leaving the copy those tools (and things like
# topgrade) track stuck on the old version forever. When set, we overwrite
# that exact file in place and skip the icon/.desktop entry below: the
# integration tool already owns and manages its own.
EXTERNALLY_MANAGED=0
if [ -n "${TARGET_APPIMAGE:-}" ]; then
    APPIMAGE_DEST="$TARGET_APPIMAGE"
    EXTERNALLY_MANAGED=1
fi

# Create directories if they don't exist
mkdir -p "$APPLICATIONS_DIR"
mkdir -p "$ICONS_DIR"
mkdir -p "$APPLICATIONS_DESKTOP_DIR"
if [ "$EXTERNALLY_MANAGED" -eq 1 ]; then
    mkdir -p "$(dirname "$APPIMAGE_DEST")"
else
    APPIMAGE_DEST="$APPLICATIONS_DIR/$APPIMAGE_NAME"
fi

# Download AppImage to a sibling temp file, then atomically rename it into
# place. Writing directly to the destination fails with ETXTBSY ("Text file
# busy") when the currently-running AppImage is still being unmounted by the
# kernel. rename(2) on the same filesystem only swaps the directory entry —
# the running process keeps its open inode, so there is no conflict.
APPIMAGE_TMP="$APPIMAGE_DEST.new"
echo "Downloading AppImage..."
if command -v curl &>/dev/null; then
    curl -L -o "$APPIMAGE_TMP" "$APPIMAGE_URL"
elif command -v wget &>/dev/null; then
    wget -O "$APPIMAGE_TMP" "$APPIMAGE_URL"
else
    echo "Error: neither curl nor wget found. Please install one of them." >&2
    exit 1
fi

chmod +x "$APPIMAGE_TMP"
mv -f "$APPIMAGE_TMP" "$APPIMAGE_DEST"
echo "AppImage installed to $APPIMAGE_DEST (executable)."

if [ "$EXTERNALLY_MANAGED" -eq 1 ]; then
    echo ""
    echo "Updated in place — leaving the icon and .desktop entry to the tool"
    echo "that manages this AppImage (Gear Lever, AppImageLauncher, etc.)."
    exit 0
fi

# Download icon
echo "Downloading icon..."
if command -v curl &>/dev/null; then
    curl -L -o "$ICONS_DIR/$ICON_NAME" "$ICON_URL"
elif command -v wget &>/dev/null; then
    wget -O "$ICONS_DIR/$ICON_NAME" "$ICON_URL"
fi
echo "Icon installed to $ICONS_DIR/$ICON_NAME."

# Create .desktop entry
DESKTOP_FILE="$APPLICATIONS_DESKTOP_DIR/$DESKTOP_NAME"
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Version=${LATEST_VERSION}
Type=Application
Name=Mosaic Mod Manager
Comment=Linux Mod Manager
Exec=${APPLICATIONS_DIR}/${APPIMAGE_NAME}
Icon=${ICONS_DIR}/${ICON_NAME}
Categories=Game;Utility;
Terminal=false
EOF

echo "Desktop entry created at $DESKTOP_FILE."
echo ""
echo "Installation complete. You can launch Mosaic Mod Manager from your application menu."
