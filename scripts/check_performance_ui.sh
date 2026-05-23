#!/usr/bin/env bash
set -euo pipefail

base_url="${PERFORMANCE_BASE_URL:-http://127.0.0.1:8001}"
out_dir="${PERFORMANCE_UI_OUT_DIR:-/tmp}"

if command -v playwright >/dev/null 2>&1; then
  playwright_cmd=(playwright)
else
  cached_cli="$(find "${HOME}/.npm/_npx" -path '*/node_modules/@playwright/test/cli.js' -print -quit 2>/dev/null || true)"
  if [[ -n "${cached_cli}" ]]; then
    playwright_cmd=(node "${cached_cli}")
  else
    playwright_cmd=(npx --offline playwright)
  fi
fi

"${playwright_cmd[@]}" screenshot \
  --channel=chrome \
  --viewport-size=1440,1400 \
  '--wait-for-selector=.performance-trend-chart[data-player-id="1642272"][data-game-id="0042500312"][data-point-count="8"]' \
  --full-page \
  "${base_url}/performance" \
  "${out_dir}/nba-performance-jared-trend-chart.png"

"${playwright_cmd[@]}" screenshot \
  --channel=chrome \
  --viewport-size=1440,1400 \
  '--wait-for-selector=.performance-trend-point-item[data-trend-date="2026-05-20"][data-trend-value="12"]' \
  --full-page \
  "${base_url}/performance" \
  "${out_dir}/nba-performance-jared-trend-data-strip.png"

echo "Performance UI trend check passed for ${base_url}"
