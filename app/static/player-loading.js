import { seasonFetch } from "./season.js";
import { initPlayerContent } from "./workbench.js";
const content = document.querySelector("#player-content");
const loading = document.querySelector("#player-loading");
const status = document.querySelector("#player-load-status");
const retry = document.querySelector("#player-load-retry");
const scopedContentUrl = content.dataset.contentUrl;

async function load() {
  retry.hidden = true;
  content.setAttribute("aria-busy", "true");
  status.textContent = scopedContentUrl
    ? "Loading published player evidence…"
    : "Loading profile, game logs, and statistical context…";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 45000);
  try {
    const options = { signal: controller.signal };
    const response = scopedContentUrl
      ? await fetch(scopedContentUrl, options)
      : await seasonFetch(`/players/${content.dataset.playerId}/content`, options);
    if (!response.ok) {
      if (scopedContentUrl) {
        status.textContent = "Profile unavailable for this scope. The requested evidence could not be loaded from this publication.";
        retry.hidden = response.status === 404;
        return;
      }
      if (response.status === 404) {
        status.textContent = "Player not found for this season. Search for another player.";
        return;
      }
      throw new Error("Profile could not be loaded. Please try again.");
    }
    const html = await response.text();
    // Only our same-origin, Jinja-escaped profile fragment is inserted here.
    content.innerHTML = html;
    content.querySelectorAll("script[type='module']").forEach(script => script.remove());
    initPlayerContent();
    document.title = content.querySelector("h1")?.textContent || "Player profile";
    loading.hidden = true;
    const research = content.querySelector("[data-lazy-research]");
    research?.addEventListener("toggle", async () => {
      if (!research.open) return;
      try {
        const { initialize } = await import("./research.js");
        await initialize(research.querySelector("[data-research-root]"));
      } catch {
        research.querySelector("[data-research-status]").textContent = "Research could not load. Reload the page to retry.";
      }
    });
  } catch (error) {
    status.textContent = error.name === "AbortError" ? "The data connection is taking too long. Please try again." : "Profile could not be loaded. Please try again.";
    retry.hidden = false;
  } finally {
    clearTimeout(timeout);
    content.setAttribute("aria-busy", "false");
  }
}
retry.addEventListener("click", load);
load();
