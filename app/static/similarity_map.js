(function () {
  "use strict";

  // Warm, high-contrast palette that reads against the dark theme. Archetypes
  // cycle through these in the order the API returns them (most populous first).
  const ARCHETYPE_COLORS = [
    "#f04e23",
    "#6bc5f2",
    "#5dd39e",
    "#f0c823",
    "#c77dff",
    "#ff6b6b",
    "#4dd0e1",
    "#ffa94d",
    "#9ccc65",
    "#ba68c8",
    "#ff8a65",
    "#7986cb",
  ];

  const FONT_COLOR = "#e7e0d4";
  const GRID_COLOR = "rgba(231, 224, 212, 0.12)";
  const AXIS_BG = "rgba(0, 0, 0, 0)";
  const EDGE_COLOR = "#f0c823";
  const ANCHOR_COLOR = "#ffffff";
  const DIM_OPACITY = 0.12;
  const BASE_OPACITY = 0.85;

  const plotEl = document.getElementById("similarity-plot");
  const metaEl = document.getElementById("map-meta");
  const noteEl = document.getElementById("map-note");
  const axesNoteEl = document.getElementById("map-axes-note");
  const layoutEl = document.querySelector(".map-layout");
  const panelEl = document.getElementById("map-panel");
  const panelName = document.getElementById("map-panel-name");
  const panelArchetype = document.getElementById("map-panel-archetype");
  const panelStatus = document.getElementById("map-panel-status");
  const panelDetail = document.getElementById("map-panel-detail");
  const neighborListEl = document.getElementById("map-neighbor-list");
  const datalistEl = document.getElementById("map-player-list");
  const searchForm = document.getElementById("map-search-form");
  const searchInput = document.getElementById("map-search-input");
  const clearBtn = document.getElementById("map-clear-btn");

  if (!plotEl) {
    return;
  }

  // Populated after the projection loads.
  const playerById = new Map();
  const idByName = new Map();
  let baseTraceCount = 0;
  let extraIndices = [];
  let selectedId = null;

  function setMeta(text) {
    if (metaEl) {
      metaEl.textContent = text;
    }
  }

  function showEmpty(message) {
    plotEl.innerHTML =
      '<div class="similarity-empty meta">' + message + "</div>";
  }

  function markerSizes(players) {
    const games = players.map((p) =>
      typeof p.games_sampled === "number" ? p.games_sampled : 0,
    );
    const min = Math.min(...games);
    const max = Math.max(...games);
    if (!isFinite(min) || !isFinite(max) || max === min) {
      return players.map(() => 7);
    }
    return games.map((g) => 4 + ((g - min) / (max - min)) * 7);
  }

  function hoverText(player) {
    const lines = [];
    lines.push("<b>" + (player.player_name || "Unknown") + "</b>");
    const teamBits = [];
    if (player.team_abbr) {
      teamBits.push(player.team_abbr);
    }
    if (typeof player.games_sampled === "number") {
      teamBits.push(player.games_sampled + " games");
    }
    if (teamBits.length) {
      lines.push(teamBits.join(" · "));
    }
    if (player.archetype_label) {
      lines.push(player.archetype_label);
    }
    if (Array.isArray(player.top_traits) && player.top_traits.length) {
      lines.push(player.top_traits.join(", "));
    }
    return lines.join("<br>");
  }

  // archetype_label is per-player and granular (e.g. "Scoring Guard - Scoring
  // Volume / Recent Scoring"). Color by the base family (the part before the
  // first " - ") so the legend stays readable and meaningful.
  function baseArchetype(label) {
    const text = (label || "Unclassified").split(" - ")[0].trim();
    return text || "Unclassified";
  }

  function buildTraces(players) {
    const byLabel = new Map();
    players.forEach((player) => {
      const label = baseArchetype(player.archetype_label);
      if (!byLabel.has(label)) {
        byLabel.set(label, []);
      }
      byLabel.get(label).push(player);
    });

    // Most populous families first, so palette order tracks prominence.
    const labels = Array.from(byLabel.keys()).sort(
      (a, b) => byLabel.get(b).length - byLabel.get(a).length || a.localeCompare(b),
    );

    return labels.map((label, index) => {
      const group = byLabel.get(label);
      return {
        type: "scatter3d",
        mode: "markers",
        name: label + " (" + group.length + ")",
        x: group.map((p) => p.x),
        y: group.map((p) => p.y),
        z: group.map((p) => p.z),
        text: group.map(hoverText),
        customdata: group.map((p) => p.player_id),
        hovertemplate: "%{text}<extra></extra>",
        marker: {
          size: markerSizes(group),
          color: ARCHETYPE_COLORS[index % ARCHETYPE_COLORS.length],
          opacity: BASE_OPACITY,
          line: { width: 0 },
        },
      };
    });
  }

  function axis(title) {
    return {
      title: { text: title, font: { color: FONT_COLOR } },
      backgroundcolor: AXIS_BG,
      gridcolor: GRID_COLOR,
      zerolinecolor: GRID_COLOR,
      showbackground: true,
      tickfont: { color: FONT_COLOR, size: 10 },
    };
  }

  function variancePct(meta) {
    return typeof meta.variance === "number" && meta.variance > 0
      ? " (" + Math.round(meta.variance * 100) + "%)"
      : "";
  }

  // PCA axes are blends of all features. Label each with its single top driver
  // + the share of variance it captures, so PC1/2/3 read as "what separates
  // players" without crowding the 3D scene. The full driver list is in the
  // caption below the plot.
  function axisTitle(meta, fallback) {
    if (!meta) {
      return fallback;
    }
    const top = (meta.drivers || [])[0];
    return top
      ? fallback + " · " + top + variancePct(meta)
      : fallback + variancePct(meta);
  }

  function renderAxesCaption(axes) {
    if (!axesNoteEl) {
      return;
    }
    if (!axes.length) {
      axesNoteEl.textContent = "";
      return;
    }
    const parts = axes.map((meta, index) => {
      const drivers = (meta.drivers || []).join(", ");
      return (
        "PC" + (index + 1) + variancePct(meta) + (drivers ? " — " + drivers : "")
      );
    });
    axesNoteEl.textContent =
      "Each axis is a PCA blend of all stats: " + parts.join("; ") + ".";
  }

  function baseIndices() {
    const indices = [];
    for (let i = 0; i < baseTraceCount; i += 1) {
      indices.push(i);
    }
    return indices;
  }

  function clearExtras() {
    if (extraIndices.length) {
      window.Plotly.deleteTraces(plotEl, extraIndices);
      extraIndices = [];
    }
  }

  // The CSS grid hands the plot a narrower column when the panel opens (and the
  // full width back when it closes). Plotly only auto-resizes on window resize,
  // so nudge it after the layout reflows or the legend collides with the panel.
  function resizePlot() {
    window.requestAnimationFrame(() => window.Plotly.Plots.resize(plotEl));
  }

  function formatScore(score) {
    if (typeof score !== "number") {
      return "–";
    }
    return Math.round(score * 100) + "%";
  }

  function renderPanel(anchor, payload) {
    const neighbors = (payload.neighbors || []).filter(
      (n) => typeof n.player_id === "number" || typeof n.player_id === "string",
    );
    panelName.textContent = anchor.player_name || payload.player_name || "Player";
    panelArchetype.textContent = anchor.archetype_label || "";
    panelDetail.setAttribute("href", "/players/" + anchor.player_id);

    if (!neighbors.length) {
      panelStatus.textContent =
        payload.reason || "No similar-player matches are available.";
    } else {
      panelStatus.textContent =
        "Top " + neighbors.length + " stat-profile matches";
    }

    neighborListEl.innerHTML = "";
    neighbors.forEach((neighbor) => {
      const item = document.createElement("li");
      item.className = "map-neighbor";

      const left = document.createElement("span");
      left.className = "map-neighbor-name";
      const team = neighbor.team_abbr ? " · " + neighbor.team_abbr : "";
      left.textContent = (neighbor.player_name || "Unknown") + team;

      const right = document.createElement("span");
      right.className = "map-neighbor-score";
      right.textContent = formatScore(neighbor.similarity_score);

      item.appendChild(left);
      item.appendChild(right);
      if (playerById.has(neighbor.player_id)) {
        item.addEventListener("click", () => selectPlayer(neighbor.player_id));
      } else {
        item.style.cursor = "default";
      }
      neighborListEl.appendChild(item);
    });

    panelEl.hidden = false;
    if (layoutEl) {
      layoutEl.classList.add("has-selection");
    }
    if (clearBtn) {
      clearBtn.hidden = false;
    }
    resizePlot();
  }

  function drawSelection(anchor, payload) {
    clearExtras();

    const segX = [];
    const segY = [];
    const segZ = [];
    const nx = [];
    const ny = [];
    const nz = [];
    const nNames = [];
    const nHover = [];

    (payload.neighbors || []).forEach((neighbor) => {
      const match = playerById.get(neighbor.player_id);
      if (!match) {
        return;
      }
      segX.push(anchor.x, match.x, null);
      segY.push(anchor.y, match.y, null);
      segZ.push(anchor.z, match.z, null);
      nx.push(match.x);
      ny.push(match.y);
      nz.push(match.z);
      nNames.push(neighbor.player_name || "Unknown");
      nHover.push(
        "<b>" +
          (neighbor.player_name || "Unknown") +
          "</b><br>Similarity " +
          formatScore(neighbor.similarity_score),
      );
    });

    const edgesTrace = {
      type: "scatter3d",
      mode: "lines",
      x: segX,
      y: segY,
      z: segZ,
      line: { color: EDGE_COLOR, width: 3 },
      opacity: 0.7,
      hoverinfo: "skip",
      showlegend: false,
    };
    const neighborTrace = {
      type: "scatter3d",
      mode: "markers+text",
      x: nx,
      y: ny,
      z: nz,
      text: nNames,
      textposition: "top center",
      textfont: { color: FONT_COLOR, size: 10 },
      hovertext: nHover,
      hovertemplate: "%{hovertext}<extra></extra>",
      marker: {
        size: 7,
        color: EDGE_COLOR,
        line: { color: "#1a1a1a", width: 1 },
      },
      showlegend: false,
    };
    const anchorName = anchor.player_name || "Player";
    const anchorTrace = {
      type: "scatter3d",
      mode: "markers+text",
      x: [anchor.x],
      y: [anchor.y],
      z: [anchor.z],
      text: [anchorName],
      textposition: "top center",
      textfont: { color: ANCHOR_COLOR, size: 12 },
      hovertext: ["<b>" + anchorName + "</b>"],
      hovertemplate: "%{hovertext}<extra></extra>",
      marker: {
        size: 12,
        color: ANCHOR_COLOR,
        line: { color: "#f04e23", width: 2 },
      },
      showlegend: false,
    };

    window.Plotly.addTraces(plotEl, [edgesTrace, neighborTrace, anchorTrace]);
    extraIndices = [baseTraceCount, baseTraceCount + 1, baseTraceCount + 2];
    window.Plotly.restyle(plotEl, { "marker.opacity": DIM_OPACITY }, baseIndices());
  }

  function selectPlayer(playerId) {
    const match = playerById.get(playerId);
    if (!match) {
      return;
    }
    selectedId = playerId;
    if (searchInput) {
      searchInput.value = match.player_name || "";
    }

    fetch("/api/similarity-map/neighbors/" + encodeURIComponent(playerId))
      .then((response) => {
        if (!response.ok) {
          throw new Error("Request failed: " + response.status);
        }
        return response.json();
      })
      .then((payload) => {
        if (selectedId !== playerId) {
          return; // a newer selection won
        }
        drawSelection(match, payload);
        renderPanel(match, payload);
      })
      .catch(() => {
        drawSelection(match, { neighbors: [] });
        renderPanel(match, {
          neighbors: [],
          reason: "Could not load similar players.",
        });
      });
  }

  function clearSelection() {
    selectedId = null;
    clearExtras();
    window.Plotly.restyle(plotEl, { "marker.opacity": BASE_OPACITY }, baseIndices());
    if (panelEl) {
      panelEl.hidden = true;
    }
    if (layoutEl) {
      layoutEl.classList.remove("has-selection");
    }
    if (clearBtn) {
      clearBtn.hidden = true;
    }
    if (searchInput) {
      searchInput.value = "";
    }
    resizePlot();
  }

  function resolveSearch() {
    if (!searchInput) {
      return;
    }
    const key = searchInput.value.trim().toLowerCase();
    if (!key) {
      return;
    }
    const playerId = idByName.get(key);
    if (playerId !== undefined) {
      selectPlayer(playerId);
    }
  }

  function indexPlayers(players) {
    players.forEach((player) => {
      playerById.set(player.player_id, player);
      const name = (player.player_name || "").trim().toLowerCase();
      if (name && !idByName.has(name)) {
        idByName.set(name, player.player_id);
      }
    });
    if (datalistEl) {
      const fragment = document.createDocumentFragment();
      players.forEach((player) => {
        if (!player.player_name) {
          return;
        }
        const option = document.createElement("option");
        option.value = player.player_name;
        fragment.appendChild(option);
      });
      datalistEl.innerHTML = "";
      datalistEl.appendChild(fragment);
    }
  }

  function wireControls() {
    if (searchForm) {
      searchForm.addEventListener("submit", (event) => {
        event.preventDefault();
        resolveSearch();
      });
    }
    if (searchInput) {
      searchInput.addEventListener("change", resolveSearch);
    }
    if (clearBtn) {
      clearBtn.addEventListener("click", clearSelection);
    }
  }

  function render(data) {
    const players = (data.players || []).filter(
      (p) =>
        typeof p.x === "number" &&
        typeof p.y === "number" &&
        typeof p.z === "number",
    );

    if (!players.length) {
      showEmpty(
        "The similarity projection is not available yet. It is published after the next pipeline run.",
      );
      setMeta("No projection data");
      return;
    }

    indexPlayers(players);
    const traces = buildTraces(players);
    baseTraceCount = traces.length;

    const axes = Array.isArray(data.axes) ? data.axes : [];
    renderAxesCaption(axes);

    const layout = {
      paper_bgcolor: AXIS_BG,
      plot_bgcolor: AXIS_BG,
      font: { color: FONT_COLOR },
      margin: { l: 0, r: 0, t: 0, b: 0 },
      showlegend: true,
      legend: {
        font: { color: FONT_COLOR, size: 11 },
        bgcolor: AXIS_BG,
        itemsizing: "constant",
      },
      scene: {
        xaxis: axis(axisTitle(axes[0], "PC1")),
        yaxis: axis(axisTitle(axes[1], "PC2")),
        zaxis: axis(axisTitle(axes[2], "PC3")),
        aspectmode: "cube",
      },
    };

    const config = { responsive: true, displaylogo: false };
    window.Plotly.newPlot(plotEl, traces, layout, config);

    plotEl.on("plotly_click", (event) => {
      if (!event || !event.points || !event.points.length) {
        return;
      }
      const point = event.points[0];
      // Ignore clicks on the overlay (edge/highlight) traces.
      if (point.curveNumber >= baseTraceCount) {
        return;
      }
      if (point.customdata) {
        selectPlayer(point.customdata);
      }
    });

    wireControls();
    setMeta(players.length + " players · " + traces.length + " archetypes");
  }

  function init() {
    if (!window.Plotly) {
      showEmpty("3D rendering library failed to load.");
      setMeta("Unavailable");
      return;
    }
    fetch("/api/similarity-map")
      .then((response) => {
        if (!response.ok) {
          throw new Error("Request failed: " + response.status);
        }
        return response.json();
      })
      .then(render)
      .catch(() => {
        showEmpty("Could not load the similarity projection.");
        setMeta("Error");
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
