import { seasonFetch, selectedSeason, withSeason } from "./season.js";

const form = document.querySelector("#players-search-form");
const input = document.querySelector("#players-search");
const grid = document.querySelector("#players-grid");
const status = document.querySelector("#players-status");
const heading = document.querySelector("#players-heading");
const description = document.querySelector("#players-description");
const reset = document.querySelector("#players-reset");
const retry = document.querySelector("#players-retry");
let controller;
let currentQuery = "";

function node(tag, className, text) {
  const el = document.createElement(tag);
  el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function playerCard(player, searching) {
  const card = node("a", "player-directory-card");
  card.href = withSeason(`/players/${encodeURIComponent(player.player_id)}`);
  const identity = node("div", "player-card-identity");
  const avatar = node("span", "player-card-avatar", player.player_initials || player.player_name.split(/\s+/).map(part => part[0]).slice(0, 2).join(""));
  if (player.headshot_url) {
    const img = document.createElement("img");
    img.src = player.headshot_url;
    img.alt = "";
    img.loading = "lazy";
    avatar.replaceChildren(img);
    img.addEventListener("error", () => { avatar.textContent = player.player_initials || "NBA"; });
  }
  avatar.setAttribute("aria-hidden", "true");
  const names = node("div", "");
  names.append(node("h3", "", player.player_name), node("p", "meta", `${player.latest_team_abbr || "Team unavailable"} · ${selectedSeason()}`));
  identity.append(avatar, names);
  const sampled = searching ? player.games_sampled : player.last_5_games;
  const sample = sampled == null ? "Sample unavailable" : `${sampled} ${searching ? "season games" : "games in recent window"}`;
  const footer = node("div", "player-card-footer");
  footer.append(node("span", "", sample), node("span", "", "View profile →"));
  card.append(identity);
  if (searching && player.sample_warning) card.append(node("p", "meta", player.sample_warning));
  if (!searching && player.as_of_date) card.append(node("p", "meta", `As of ${String(player.as_of_date).slice(0, 10)}`));
  card.append(footer);
  return card;
}

async function load(query = "") {
  controller?.abort();
  const request = new AbortController();
  controller = request;
  currentQuery = query;
  reset.hidden = !query;
  retry.hidden = true;
  grid.replaceChildren();
  grid.setAttribute("aria-busy", "true");
  heading.textContent = query ? `Results for “${query}”` : "Browse players";
  description.textContent = query ? "Select a player to open their season profile and game logs." : "A starting selection from the season’s fantasy recommendation rankings. Search to find other players.";
  status.textContent = "Loading players…";
  try {
    const response = await seasonFetch(query ? `/api/players/search?q=${encodeURIComponent(query)}` : "/api/rankings?limit=12", { signal: request.signal });
    if (!response.ok) throw new Error("Players could not be loaded. Check the data connection and try again.");
    const data = await response.json();
    if (request.signal.aborted) return;
    const items = data.items || [];
    grid.replaceChildren(...items.map(player => playerCard(player, Boolean(query))));
    status.textContent = items.length ? `${items.length} ${items.length === 1 ? "player" : "players"} shown · ${selectedSeason()}${query ? " · Refine your search if your player is missing." : ""}` : query ? "No players found. Try a different name or season." : "No ranked players available for this season. Search by name to find a player.";
  } catch (error) {
    if (request.signal.aborted) return;
    status.textContent = error.message;
    retry.hidden = false;
  } finally {
    if (!request.signal.aborted) grid.setAttribute("aria-busy", "false");
  }
}

form.addEventListener("submit", event => { event.preventDefault(); load(input.value.trim()); });
reset.addEventListener("click", () => { input.value = ""; load(); input.focus(); });
input.addEventListener("search", () => { if (!input.value) load(); });
retry.addEventListener("click", () => load(currentQuery));
load();
