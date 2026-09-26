export const METRICS = [
  "pts",
  "reb",
  "ast",
  "stl",
  "blk",
  "tov",
  "fg3m",
  "min",
  "plus_minus",
  "fga",
  "fg3a",
  "fta",
  "fg_pct",
  "fg3_pct",
  "ft_pct",
  "ts_pct",
  "efg_pct",
  "pts_per36",
  "ast_per36",
];
export const displayValue = (value) =>
  value === null || value === undefined
    ? "Unavailable"
    : typeof value === "number"
      ? (Math.abs(value) < 0.05 ? 0 : value).toFixed(1)
      : String(value);
export function readScope(form, season) {
  const get = (key) => form.elements.namedItem(key).value;
  return {
    season,
    phase: get("phase"),
    player_ids: [get("player"), get("compare")].filter(Boolean).map(Number),
    metrics: METRICS,
    aggregation: get("aggregation"),
    start: get("start") || null,
    end: get("end") || null,
    opponent: get("opponent").toUpperCase() || null,
    home_away: get("home_away") || null,
    rest: get("rest") || null,
    teammate_id: get("teammate") ? Number(get("teammate")) : null,
    teammate_status: get("teammate_status") || null,
  };
}
const node = (tag, text) => {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  return e;
};
function table(headers, rows) {
  const wrap = node("div");
  wrap.className = "research-scroll";
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", "Research results table");
  const t = node("table"),
    head = node("thead"),
    hr = node("tr");
  for (const h of headers) {
    const th = node("th", h);
    th.scope = "col";
    hr.append(th);
  }
  head.append(hr);
  t.append(head);
  const body = node("tbody");
  for (const row of rows) {
    const tr = node("tr");
    for (const value of row) tr.append(node("td", displayValue(value)));
    body.append(tr);
  }
  t.append(body);
  wrap.append(t);
  return wrap;
}
async function fetchJSON(url, options) {
  const r = await fetch(url, options);
  const data = await r.json();
  if (!r.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Check the selected scope and dates.",
    );
  return data;
}
function renderBreakdown(target, data) {
  target.replaceChildren();
  for (const p of data.players) {
    target.append(node("h3", p.player_name));
    target.append(
      table(
        [
          "Stat",
          "Value",
          "Unit",
          "Aggregation",
          "Valid / observed",
          "Numerator",
          "Denominator",
        ],
        p.metrics.map((m) => [
          m.label,
          m.result?.display_value,
          m.unit,
          m.aggregation,
          `${m.result?.valid_games ?? 0} / ${m.result?.observed_games ?? 0}`,
          m.result?.numerator,
          m.result?.denominator,
        ]),
      ),
    );
    target.append(
      node(
        "p",
        `${p.filter_missing_games} games excluded for missing filter context.`,
      ),
    );
    const d = node("details");
    d.append(node("summary", `Inspect ${p.games.length} underlying games`));
    d.append(
      table(
        [
          "Date",
          "Opponent",
          "MIN",
          "PTS",
          "REB",
          "AST",
          "STL",
          "BLK",
          "TOV",
          "3PM",
          "+/-",
        ],
        p.games.map((g) => [
          g.game_date,
          g.opponent_abbr,
          g.min,
          g.pts,
          g.reb,
          g.ast,
          g.stl,
          g.blk,
          g.tov,
          g.fg3m,
          g.plus_minus,
        ]),
      ),
    );
    target.append(d);
  }
  for (const text of data.limitations) target.append(node("p", text));
  const provenance = node("details");
  provenance.append(node("summary", "Scope and source"));
  provenance.append(
    node(
      "pre",
      JSON.stringify(
        {
          scope: data.scope,
          source: data.source,
          snapshot_id: data.snapshot_id,
        },
        null,
        2,
      ),
    ),
  );
  target.append(provenance);
}
function renderStudy(target, study) {
  target.replaceChildren(
    node("h3", `${study.player_name} · ${study.teammate_name} availability`),
  );
  target.append(
    node(
      "p",
      study.scope
        ? `${study.scope.season} · ${study.scope.start} through ${study.scope.end}. Differences: reported Out minus participated; causal columns use unavailable minus available.`
        : "Coverage pending. No reviewed study has been published for this pair.",
    ),
  );
  target.append(
    table(
      [
        "Stat",
        "Participated",
        "Reported Out",
        "Observed Δ",
        "Adjusted association",
        "Causal estimate",
        "Change unit",
        "Valid games: with / out",
        "Evidence",
      ],
      study.metrics.map((m) => [
        m.label || m.metric.toUpperCase(),
        m.descriptive?.participated,
        m.descriptive?.reported_out,
        m.descriptive?.difference,
        m.association?.adjusted_difference,
        m.causal?.estimate,
        m.unit,
        m.descriptive?.groups?.map((g) => g.valid_games).join(" / "),
        m.status.replaceAll("_", " "),
      ]),
    ),
  );
  const details = node("details");
  details.append(node("summary", "Samples, uncertainty, and evidence limits"));
  for (const m of study.metrics) {
    const a = m.association,
      c = m.causal;
    details.append(
      node(
        "p",
        `${m.metric.toUpperCase()}: ${m.reason || m.status}. Valid appearances: ${m.descriptive?.groups?.map((g) => g.valid_games).join(" / ") ?? "unavailable"}. Adjusted episodes: ${a?.episodes ?? "unavailable"}. Causal interval: ${c?.interval?.map(displayValue).join(" to ") ?? "unavailable"}. Association interval (nominal): ${a?.nominal_uncertainty?.cluster?.nominal_95_ci?.map(displayValue).join(" to ") ?? "unavailable"}.`,
      ),
    );
  }
  for (const text of [
    ...study.limitations,
    ...(study.membership_assumptions || []),
  ])
    details.append(node("p", text));
  target.append(details);
}
export async function initialize(root) {
  if (root.dataset.researchInitialized) return;
  root.dataset.researchInitialized = "true";
  const form = root.querySelector("form"),
    status = root.querySelector("[data-research-status]"),
    season = root.dataset.season;
  const urlSeason = `?season=${encodeURIComponent(season)}`;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector("[type=submit]");
    submit.disabled = true;
    status.textContent = "Loading breakdown…";
    try {
      const scope = readScope(form, season);
      const data = await fetchJSON(`/api/research/breakdown${urlSeason}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(scope),
      });
      renderBreakdown(root.querySelector("[data-research-results]"), data);
      const params = new URLSearchParams(location.search);
      for (const control of form.elements)
        if (control.name) {
          if (control.value) params.set(control.name, control.value);
          else params.delete(control.name);
        }
      params.set("season", season);
      history.replaceState(null, "", `${location.pathname}?${params}`);
      root.querySelector('a[href^="/research"]').href = `/research?${params}`;
      status.textContent = "Breakdown ready.";
    } catch (error) {
      status.textContent = error.message;
      root.querySelector("[data-research-results]").replaceChildren();
    } finally {
      submit.disabled = false;
    }
  });
  const studiesTask = fetchJSON("/api/research/studies")
    .then(({ studies }) => {
      const selector = root.querySelector("[data-study-selector]");
      for (const study of studies) {
        const b = node(
          "button",
          `${study.player_name} / ${study.teammate_name}`,
        );
        b.type = "button";
        b.setAttribute("aria-pressed", "false");
        b.addEventListener("click", () => {
          for (const button of selector.children)
            button.setAttribute("aria-pressed", String(button === b));
          renderStudy(root.querySelector("[data-study-results]"), study);
        });
        selector.append(b);
      }
      selector.firstElementChild?.click();
    })
    .catch((error) => {
      root.querySelector("[data-study-results]").textContent = error.message;
    });
  try {
    const { players } = await fetchJSON(`/api/research/players${urlSeason}`);
    const query = new URLSearchParams(location.search);
    for (const name of ["player", "compare", "teammate"]) {
      const select = form.elements.namedItem(name);
      select.replaceChildren(
        node("option", name === "player" ? "Choose player" : "None"),
      );
      select.firstElementChild.value = "";
      for (const p of players.sort((a, b) =>
        a.player_name.localeCompare(b.player_name),
      )) {
        const o = node("option", p.player_name);
        o.value = p.player_id;
        select.append(o);
      }
    }
    form.elements.namedItem("player").value =
      root.dataset.playerA || query.get("player") || "";
    form.elements.namedItem("compare").value =
      root.dataset.playerB || query.get("compare") || "";
    for (const key of [
      "phase",
      "start",
      "end",
      "opponent",
      "home_away",
      "rest",
      "aggregation",
      "teammate",
      "teammate_status",
    ])
      if (query.has(key)) form.elements.namedItem(key).value = query.get(key);
    status.textContent = "Choose a scope to inspect the underlying games.";
  } catch (error) {
    status.textContent = error.message;
  }
  await studiesTask;
}
if (typeof document !== "undefined")
  for (const root of document.querySelectorAll("[data-research-root]"))
    if (!root.closest("[data-lazy-research]")) initialize(root);
