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
