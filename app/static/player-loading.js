import { seasonFetch } from "./season.js";
// Match the versioned module already loaded by base.html: a second module URL
// would install duplicate document-level handlers.
const version = new URL(import.meta.url).searchParams.get("v");
const { initPlayerContent } = await import(`./workbench.js?v=${encodeURIComponent(version)}`);
const content = document.querySelector("#player-content");
const loading = document.querySelector("#player-loading");
const status = document.querySelector("#player-load-status");
const retry = document.querySelector("#player-load-retry");

async function load() {
  retry.hidden = true;
  content.setAttribute("aria-busy", "true");
  status.textContent = "Loading profile, game logs, and statistical context…";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 45000);
  try {
    const response = await seasonFetch(`/players/${content.dataset.playerId}/content`, { signal: controller.signal });
    if (!response.ok) {
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
        const { initialize } = await import(`./research.js?v=${encodeURIComponent(version)}`);
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
