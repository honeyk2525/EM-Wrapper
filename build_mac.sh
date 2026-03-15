#!/usr/bin/env bash
# =============================================================================
# build_mac.sh — Build GROMACS Runner as a macOS .app bundle and .pkg installer
#
# Usage:
#   chmod +x build_mac.sh
#   ./build_mac.sh
#
# Output:
#   dist/GROMACS Runner.app   — standalone app bundle (drag to /Applications)
#   dist/GROMACS_Runner.pkg   — installer package   (double-click to install)
#
# Requirements:
#   - macOS (Apple Silicon or Intel)
#   - Python 3.8+ with pip3
#   - Xcode Command Line Tools: xcode-select --install
# =============================================================================

set -euo pipefail

APP_NAME="GROMACS Runner"
BUNDLE_ID="com.gromacsrunner.app"
VERSION="1.0.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"
BUILD_DIR="$SCRIPT_DIR/build"

echo "======================================================"
echo "  GROMACS Runner — macOS build"
echo "======================================================"
echo ""

# ── 1. Check for Python ────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "✗ python3 not found. Install via: brew install python3"
    exit 1
fi
PYTHON=$(command -v python3)
echo "✓ Python: $($PYTHON --version)"

# ── 2. Install / upgrade PyInstaller ──────────────────────────────────────
echo ""
echo "── Installing PyInstaller ────────────────────────────"
"$PYTHON" -m pip install --quiet --upgrade pyinstaller
echo "✓ PyInstaller: $("$PYTHON" -m pyinstaller --version)"

# ── 3. Clean previous build artefacts ─────────────────────────────────────
echo ""
echo "── Cleaning previous build ───────────────────────────"
rm -rf "$BUILD_DIR" "$DIST_DIR" "$SCRIPT_DIR/__pycache__"
rm -f  "$SCRIPT_DIR/$APP_NAME.spec"
echo "✓ Clean"

# ── 4. Build the .app bundle with PyInstaller ─────────────────────────────
echo ""
echo "── Building .app bundle ──────────────────────────────"
"$PYTHON" -m pyinstaller \
    --windowed \
    --onefile \
    --name "$APP_NAME" \
    --osx-bundle-identifier "$BUNDLE_ID" \
    --distpath "$DIST_DIR" \
    --workpath "$BUILD_DIR" \
    --specpath "$SCRIPT_DIR" \
    "$SCRIPT_DIR/em_mac.py"

APP_BUNDLE="$DIST_DIR/$APP_NAME"
if [[ ! -f "$APP_BUNDLE" ]]; then
    echo "✗ Build failed — expected binary at: $APP_BUNDLE"
    exit 1
fi
echo "✓ Built: $APP_BUNDLE"

# ── 5. Wrap binary into a proper .app structure ────────────────────────────
echo ""
echo "── Wrapping into .app bundle ─────────────────────────"
APP_DIR="$DIST_DIR/$APP_NAME.app"
MACOS_DIR="$APP_DIR/Contents/MacOS"
RESOURCES_DIR="$APP_DIR/Contents/Resources"
INFO_PLIST="$APP_DIR/Contents/Info.plist"

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp "$APP_BUNDLE" "$MACOS_DIR/$APP_NAME"
chmod +x "$MACOS_DIR/$APP_NAME"
rm "$APP_BUNDLE"

cat > "$INFO_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>             <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>      <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>       <string>${BUNDLE_ID}</string>
    <key>CFBundleVersion</key>          <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key><string>${VERSION}</string>
    <key>CFBundleExecutable</key>       <string>${APP_NAME}</string>
    <key>CFBundlePackageType</key>      <string>APPL</string>
    <key>LSMinimumSystemVersion</key>   <string>11.0</string>
    <key>NSHighResolutionCapable</key>  <true/>
    <key>NSRequiresAquaSystemAppearance</key><false/>
</dict>
</plist>
PLIST

echo "✓ App bundle: $APP_DIR"

# ── 6. Build the .pkg installer ───────────────────────────────────────────
echo ""
echo "── Building .pkg installer ───────────────────────────"
PKG_ROOT="$BUILD_DIR/pkg_root"
mkdir -p "$PKG_ROOT/Applications"
cp -r "$APP_DIR" "$PKG_ROOT/Applications/"

PKG_OUT="$DIST_DIR/GROMACS_Runner_${VERSION}.pkg"
pkgbuild \
    --root        "$PKG_ROOT" \
    --identifier  "$BUNDLE_ID" \
    --version     "$VERSION" \
    --install-location "/" \
    "$PKG_OUT"

echo "✓ Installer: $PKG_OUT"

# ── 7. Clean up intermediate files ────────────────────────────────────────
rm -rf "$BUILD_DIR" "$SCRIPT_DIR/__pycache__" "$SCRIPT_DIR/$APP_NAME.spec"

echo ""
echo "======================================================"
echo "  Build complete!"
echo "  App:       dist/$APP_NAME.app"
echo "  Installer: dist/GROMACS_Runner_${VERSION}.pkg"
echo ""
echo "  To install: double-click the .pkg"
echo "  To run directly: open dist/$APP_NAME.app"
echo "======================================================"
