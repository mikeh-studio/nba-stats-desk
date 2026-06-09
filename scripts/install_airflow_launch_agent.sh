#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.nba-gcp.airflow-scheduler"
PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
START_SCRIPT="${REPO_ROOT}/scripts/start_airflow_scheduler.sh"

mkdir -p "${PLIST_DIR}" "${REPO_ROOT}/logs"

cat >"${PLIST_PATH}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${START_SCRIPT}</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>${REPO_ROOT}/logs/airflow-scheduler.launchd.out.log</string>

  <key>StandardErrorPath</key>
  <string>${REPO_ROOT}/logs/airflow-scheduler.launchd.err.log</string>
</dict>
</plist>
PLIST

chmod +x "${START_SCRIPT}"
launchctl bootout "gui/$(id -u)" "${PLIST_PATH}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_PATH}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"
echo "Installed ${LABEL} at ${PLIST_PATH}"
