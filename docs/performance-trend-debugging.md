# Performance Trend Debugging Notes

## What Went Wrong

The first fixes validated the wrong things. The API was checked separately, and
the browser smoke test only waited for an SVG node. That proved an element
existed, but it did not prove the user-facing UI had real trend rows populated.

The backend also had a masking bug: if the 30-day trend query returned no rows,
it synthesized a one-game trend from the selected box score. That made failures
look partially successful and delayed finding the real data path issue.

The frontend then added two more sources of confusion:

- Detail responses were cached in memory, so an old empty trend payload could
  keep rendering after the backend was fixed.
- The static script URL did not change after every frontend fix, so a browser
  could keep running stale `performance.js`.

Finally, the chart was only visual. If SVG rendering, layout, or stale JS hid
the line, there was no plain rendered trend-data surface to inspect.

## Fixes Applied

- The trend query is anchored on the selected `game_id` and fetches real
  `fct_player_game_stats` rows in the selected game's prior 30-day window.
- The synthetic selected-game fallback was removed.
- Performance-page fetches use `cache: "no-store"`.
- The detail cache was removed from `performance.js`.
- The script URL was versioned to force fresh browser JS.
- The trend chart now exposes `data-point-count`, `data-player-id`, and
  `data-game-id` for UI verification.
- A compact game-by-game trend data strip renders under the chart.
- `scripts/check_performance_ui.sh` verifies that Jared McCain's rendered UI has
  8 trend points and that the `2026-05-20` row renders as 12 points.

## Future Guardrail

For UI data bugs, verify the rendered DOM, not only the API. The acceptance
check should assert a user-visible data count and at least one known date/value
from the browser-rendered page.

Run the UI check with a local server:

```bash
PERFORMANCE_BASE_URL=http://127.0.0.1:8001 scripts/check_performance_ui.sh
```
