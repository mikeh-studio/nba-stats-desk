// Tabs always start with the latest supported season. Historical Ask scope is
// carried by the question and conversation, never by a sticky navigation filter.
export const DEFAULT_SEASON = "2026-27";

export function selectedSeason() {
  return DEFAULT_SEASON;
}

export function withSeason(input, season = selectedSeason()) {
  if (typeof input !== "string" || input.startsWith("#")) return input;
  const base = globalThis.location?.href || "http://localhost/";
  const url = new URL(input, base);
  if (url.origin !== new URL(base).origin) return input;
  if (season === selectedSeason()) url.searchParams.delete("season");
  else url.searchParams.set("season", season);
  return input.startsWith("http") ? url.href : url.pathname + url.search + url.hash;
}

export function seasonFetch(input, options) {
  return globalThis.fetch(withSeason(input), options);
}

export function playerSearchHref(player) {
  const path = `/players/${encodeURIComponent(player.player_id)}`;
  const supplied = player.profile_url;
  if (typeof supplied === "string" && supplied.startsWith(`${path}?`)) {
    const params = new URLSearchParams(supplied.slice(path.length));
    if (params.size === 2 && /^20\d{2}-\d{2}$/.test(params.get("season"))
      && /^pub_[a-f0-9]{16}$/.test(params.get("publication_id"))) {
      return `${path}?${params}`;
    }
  }
  return withSeason(path);
}
