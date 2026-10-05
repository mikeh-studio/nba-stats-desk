"""Shared, plain-language presentation of metric sample completeness."""

GAMES_IN_SCOPE = "Games in scope"
GAMES_WITH_DATA = "Games with data"
GAME_COVERAGE_NOTE = (
    "Games with data counts games with all the information needed for a stat, "
    "out of all games in that group. Counts can differ by stat. "
    "Complete data can still yield an unavailable rate when its denominator is zero."
)


def game_coverage_label(group: str) -> str:
    return f"{GAMES_WITH_DATA} — {group}"


def game_coverage(valid_games: int, observed_games: int) -> str:
    return f"{valid_games} of {observed_games}"
