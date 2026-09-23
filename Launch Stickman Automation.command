#!/bin/bash
# Launch Stickman Automation on macOS (same role as "Launch Stickman Automation.bat" on Windows).
cd "$(dirname "$0")" || exit 1

open_app() {
  local app="$1"
  if [ -d "$app" ]; then
    open "$app"
    exit 0
  fi
}

# Prefer a built Electron app from electron-builder output layouts.
open_app "./dist/mac/Stickman Automation.app"
open_app "./dist/mac-arm64/Stickman Automation.app"
open_app "./dist/mac-universal/Stickman Automation.app"
# Unpacked / renamed variants
for candidate in ./dist/*/Bubble\ Pod.app; do
  open_app "$candidate"
done

if command -v npm >/dev/null 2>&1; then
  echo "Starting Stickman Automation via npm start…"
  exec npm start
fi

echo "Stickman Automation was not found."
echo "On a Mac, build with:  npm run dist:mac"
echo "Or install Node and run:  npm start"
echo "Also install Python 3.11+ and:  pip install -r requirements.txt"
echo "For lip-sync, open the Gentle app (or set Gentle URL in Settings)."
read -r -p "Press Return to close…"
exit 1
