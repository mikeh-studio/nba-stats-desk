// Exact canonical evidence -> final DOM relationships; rendered text is never an oracle.
export const UI_CLAIM_VERSION = "ask_ui_claim_bindings/1";
const finite = Number.isFinite,
  clone = (v) => structuredClone(v);
const equal = (a, b) => {
  if (Object.is(a, b)) return true;
  if (
    a === null ||
    b === null ||
    typeof a !== "object" ||
    typeof b !== "object" ||
    Array.isArray(a) !== Array.isArray(b)
  )
    return false;
  const keys = Object.keys(a);
  return (
    keys.length === Object.keys(b).length &&
    keys.every((k) => Object.hasOwn(b, k) && equal(a[k], b[k]))
  );
};
function assertFiniteTree(value) {
  if (typeof value === "number" && !finite(value))
    throw Error("Nonfinite canonical evidence");
  if (value && typeof value === "object")
    Object.values(value).forEach(assertFiniteTree);
}
const num = (v, d = 1, zero = false) =>
  finite(v) ? (zero && Math.abs(v) < 0.05 ? 0 : v).toFixed(d) : "Unavailable";
const ordinal = (v) => {
  const n = Math.round(v),
    r = n % 100;
  return `${n}${r >= 11 && r <= 13 ? "th" : { 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th"}`;
};
const date = (v) =>
  /^\d{4}-\d{2}-\d{2}$/.test(v || "")
    ? new Intl.DateTimeFormat("en-US", {
        month: "short",
        day: "numeric",
        year: "numeric",
        timeZone: "UTC",
      }).format(new Date(`${v}T00:00:00Z`))
    : String(v || "");
const suffix = (u) =>
  u === "percent"
    ? "%"
    : u === "minutes per game"
      ? " min"
      : u === "per 36 minutes"
        ? " /36"
        : "";
const fmt = (v, u) => num(v) + (finite(v) && u === "percent" ? "%" : "");
export function claimSource(document, path) {
  let v = document;
  for (const k of path) {
    if (
      v === null ||
      typeof v !== "object" ||
      !Object.hasOwn(v, k) ||
      ["__proto__", "constructor", "prototype"].includes(k)
    )
      throw Error("Missing or unsafe canonical source");
    v = v[k];
  }
  return v;
}
export function prepareClaimEvidence(payload) {
  return clone(
    Object.fromEntries(
      ["semantic_evidence", "player_profile", "player_profiles"]
        .filter((k) => Object.hasOwn(payload, k))
        .map((k) => [k, payload[k]]),
    ),
  );
}
export const UI_CLAIM_GAPS = {
  overview: [
    "Generated prose and takeaways",
    "Overview time-series, profiles, generated sample prose",
  ],
  comparison: ["Generated prose, profile cards, methodology prose"],
  drawer: [
    "Freeform percentile methodology note and legacy missing sample-count fields; data-source truth and statistical methodology remain separate from presentation fidelity",
  ],
  legacy: [
    "Other routes, generic charts/tables, comparison explorer, standalone Research",
  ],
};
export function formatClaim(v, f, c = {}) {
  const m = c.metric || {},
    e = c.evidence || {};
  switch (f) {
    case "abbreviation":
      return (
        {
          pts: "PTS",
          reb: "REB",
          ast: "AST",
          stl: "STL",
          blk: "BLK",
          tov: "TOV",
          min: "MIN",
          fg3m: "3PM",
          fg3a: "3PA",
          fg3_pct: "3P%",
          fga: "FGA",
          fta: "FTA",
          fg_pct: "FG%",
          ft_pct: "FT%",
          efg_pct: "eFG%",
          ts_pct: "TS%",
          pts_per36: "PTS/36",
          ast_per36: "AST/36",
        }[v] ||
        m.label ||
        v
      );
    case "detail-label":
      return `${v}${c.unit}`;
    case "peer-heading":
      return `${formatClaim(c.metricKey, "abbreviation", { metric: { label: c.label } })} · ${v.kind === "position" ? { Guard: "Guards", Wing: "Wings/forwards", Big: "Bigs/centers" }[v.group] || v.group : "League-wide"}`;
    case "peer-standing":
      return `${v.mode === "ordinal" ? `${v.tied ? "Tied " : ""}${ordinal(v.rank)} of ${v.cohort_size}` : `${v.percentile.toFixed(1)}% of other qualifiers below this value`} · ${v.cohort_size} qualifiers · ${v.season || ""} ${v.phases.join(" + ")}`;
    case "peer-sample":
      return `${v.start} through ${v.end} · at least ${v.qualification.min_games} complete appearances${finite(v.classified_players) ? ` · ${v.classified_players}/${v.eligible_players} eligible players classified` : ""}`;
    case "peer-bin":
      return `${v.start.toFixed(1)} to ${v.end.toFixed(1)}: ${v.count} players`;
    case "peer-bin-geometry":
      return String(
        {
          x: (c.index * 240) / c.length,
          y: 60 - (55 * v.count) / c.max,
          width: Math.max(1, 240 / c.length - 1),
          height: (55 * v.count) / c.max,
        }[c.coordinate],
      );
    case "variation-count":
      return `${v} complete appearances · one dot per game · shaded middle 50% · dashed median · gold highs${c.truncated ? " · latest " + c.displayed + " games plotted; summaries and high use the full sample" : ""}`;
    case "variation-geometry": {
      const y = (n) => 112 - (90 * (n - c.min)) / (c.max - c.min || 1);
      return String(
        c.coordinate === "height" ? Math.max(1, y(v) - y(c.q3)) : y(v),
      );
    }
    case "text":
      return String(v ?? "");
    case "number":
      return num(v);
    case "ordinal":
      return ordinal(v);
    case "meter":
      return String(Math.round(v));
    case "overview-meter":
      return String(Math.round(Math.max(0, Math.min(100, v))));
    case "overview-value":
      return (
        (finite(v) ? num(v, 1, true) + suffix(m.unit || "per game") : "—") +
        (m.sample?.status === "limited" ? " Limited" : "")
      );
    case "overview-value-aria":
      return finite(v)
        ? `${num(v, 1, true)} ${m.unit || "per game"}`
        : "Unavailable";
    case "change":
      return finite(v)
        ? (v >= 0.05 ? "+" : "") +
            num(v, 1, true) +
            (m.change_unit === "percentage points"
              ? " pp"
              : (m.change_unit || m.unit) === "percent"
                ? ""
                : suffix(m.change_unit || m.unit || "per game"))
        : "—";
    case "change-aria":
      return finite(v)
        ? `${num(v, 1, true)} ${m.change_unit || m.unit || "per game"}`
        : "Comparison unavailable";
    case "comparison-value":
      return (
        num(v) +
        (m.missing_component_games
          ? `Partial: ${m.valid_games}/${m.observed_games} games`
          : "")
      );
    case "comparison-edge":
      return v === null
        ? "Unavailable"
        : v === 0
          ? "Tied"
          : `${c.names[v > 0 ? 0 : 1]} by ${num(Math.abs(v))}`;
    case "scope":
      return `${e.scope.phases.join(" + ")} · ${date(e.scope.start)} – ${date(e.scope.end)}`;
    case "overview-scope":
      return `Versus ${(e.scope.previous_phases || e.scope.phases).join(" + ")} · ${date(e.scope.previous_start)} – ${date(e.scope.previous_end)}`;
    case "detail-value":
      return (
        num(v) +
        (c.unit === "percent" && finite(v) ? "%" : "") +
        `${m.valid_games} of ${m.observed_games} games${m.missing_component_games ? " · partial inputs" : ""}` +
        (c.unit === "percent"
          ? Object.entries(m.components || {})
              .filter(([k]) => ["fga", "fg3a", "fta"].includes(k))
              .map(
                ([k, x]) =>
                  `${num(x, 0)} ${k === "fg3a" ? "3PA" : k.toUpperCase()}`,
              )
              .join(" · ")
          : "")
      );
    case "impact":
      return num(v, c.key === "ws48" ? 3 : 1);
    case "chart-value":
      return num(v.missing_component_games ? null : v.value);
    case "chart-title":
      return `${v} per game comparison`;
    case "chart-aria":
      return c.names
        .map((n, i) => `${n}: ${num(c.available[i])} ${m.label} per game`)
        .join("; ");
    case "chart-width":
      return `${Math.max(0, (v / c.max) * 100)}%`;
    case "chart-axis":
      return c.factor === 0 ? "0" : num(c.max * c.factor);
    case "drawer-title":
      return `${m.display_label || m.label} · ${c.name}`;
    case "drawer-scope":
      return `${date(e.scope.start)} – ${date(e.scope.end)} · ${e.scope.phases.join(" + ")}`;
    case "drawer-value":
      return fmt(v, m.unit);
    case "drawer-count":
      if (!finite(v) || v < 0)
        throw Error("Unavailable canonical sample count");
      return String(v);
    case "drawer-quartiles":
      return finite(v)
        ? `${fmt(v, m.unit)} – ${fmt(c.summary.q3, m.unit)}`
        : "Unavailable";
    case "drawer-rank":
      return c.view === "across_games"
        ? `Period ${m.unit === "percent" ? "pooled percentage" : "average"}`
        : finite(v)
          ? `${ordinal(v)} percentile`
          : m.percentile_label || "Not ranked";
    case "drawer-axis":
      return c.coordinate === "count"
        ? String(c.max)
        : fmt(c.coordinate === "lo" ? c.lo : c.hi, m.unit);
    case "drawer-chart-label":
      return `${m.display_label || m.label} ${c.view === "among_players" ? "distribution of qualified player values" : "individual game values; shaded middle 50 percent"}`;
    case "drawer-position": {
      if (!finite(v) || (c.coordinate === "width" && !finite(c.q3)))
        throw Error("Unavailable canonical chart position");
      const x = (n) => 24 + ((n - c.lo) / (c.hi - c.lo)) * 312;
      return String(
        c.coordinate === "width" ? Math.max(1, x(c.q3) - x(v)) : x(v),
      );
    }
    case "point-index":
      return String(c.index);
    case "drawer-point-y":
      return String(183 + (c.index % 3) * 4);
    case "drawer-point-label": {
      const opportunity =
        m.key === "ts_pct" &&
        v.components &&
        finite(v.components.fga) &&
        finite(v.components.fta)
          ? `${v.components.fga} FGA, ${v.components.fta} FTA`
          : finite(v.attempts)
            ? `${v.attempts} ${{ fga: "FGA", fta: "FTA", fg3a: "3PA" }[v.attempt_field] || v.attempt_field || "attempts"}`
            : "";
      return `${v.date}${c.separator === "title" ? " · " : ": "}${fmt(v.value, m.unit)}${opportunity ? (c.separator === "title" ? " · " : ", ") + opportunity : ""}`;
    }
    case "drawer-option":
      return `${v.date} · ${fmt(v.value, m.unit)}`;
    case "drawer-sample": {
      const count = (n) => (finite(n) && n >= 0 ? n : 0);
      return `${count(v.observed_games)} observed appearances; ${count(v.missing_games)} with missing components; ${count(v.zero_denominator_games)} with zero denominators omitted.${v.points_truncated ? " Showing the latest " + count(v.displayed_games) + " dots; summaries use all valid games." : ""} ${m.unit === "percent" ? "Game percentage medians differ from the pooled period percentage. Hover or focus a dot for its attempts." : "Zero-event games remain observations."}`;
    }
    case "drawer-qualification": {
      const policy = v.percentile_policy || {},
        attempts = policy.attempt_field
          ? v.components?.[policy.attempt_field]
          : null;
      return `${v.percentile_label || "Not qualified"}${finite(attempts) && policy.min_attempts ? " · " + attempts + " attempts; " + policy.min_attempts + " required" : ""}. ${finite(v.value) && v.distribution?.among_players?.summary?.n ? "Not included in the qualified cohort; the dashed marker shows your recorded value without a rank." : "The period value or qualified cohort is unavailable; no marker comparison is shown."}`;
    }
    case "variation-point-label":
      return `${v.date}${v.opponent ? " vs " + v.opponent : ""}: ${v.value} ${formatClaim(c.metricKey, "abbreviation", { metric: { label: c.label } })}`;
    case "variation-point-x":
      return String(
        28 + (440 * (Date.parse(v) - c.low)) / (c.high - c.low || 1),
      );
    case "variation-point-radius":
      return String(v === c.max ? 4 : 2.5);
    case "variation-point-color":
      return v === c.max ? "#e7b45b" : "currentColor";
    case "bin-title":
      return `${fmt(v.start, m.unit)} to ${fmt(v.end, m.unit)}: ${v.count} ${c.view === "among_players" ? "qualified players" : "valid games"}`;
    case "bin-geometry": {
      const x = (n) => 24 + ((n - c.lo) / (c.hi - c.lo)) * 312;
      return String(
        {
          x: x(v.start),
          y: 174 - (v.count / c.max) * 126,
          width: Math.max(1, x(v.end) - x(v.start) - 2),
          height: (v.count / c.max) * 126,
        }[c.coordinate],
      );
    }
    default:
      throw Error("Unknown claim format");
  }
}
// Fixed component schemas regenerate both destinations and semantic coordinates.
// Descriptors cannot select an equal-valued different metric/player/side/format.
export function claimSchema(payload, component, options = {}) {
  const e = payload.semantic_evidence;
  if (!e?.metrics) return [];
  if (
    component === "overview" &&
    e.metrics.some((m) => !Object.hasOwn(m, "value"))
  )
    return []; // preserved incomplete legacy records
  const records = [],
    base = ["semantic_evidence"],
    names = payload.player_profiles?.map((p) => p.player.player_name) || [];
  const identity =
    e.player_id ??
    e.players ??
    payload.player_profiles?.map((p) => ({
      player_id: p.player.player_id,
      player_name: p.player.player_name,
    }));
  const add = (selector, source, format, context = {}, attribute = null) => {
    const compact = { ...context };
    if (context.metric) {
      let path = source.slice();
      while (path.length && claimSource(payload, path) !== context.metric)
        path.pop();
      if (!path.length && Number.isInteger(context.metricIndex)) {
        path = base.concat("metrics", context.metricIndex);
        if (context.side && claimSource(payload, path) !== context.metric)
          path.push(context.side);
      }
      if (!path.length || claimSource(payload, path) !== context.metric)
        throw Error("Metric not anchored to source");
      compact.metricPath = path;
      delete compact.metric;
    }
    if (context.summary) {
      compact.summaryPath = base.concat(
        "metrics",
        context.metricIndex,
        "distribution",
        context.view,
        "summary",
      );
      delete compact.summary;
    }
    if (context.names) {
      compact.namesPath = ["player_profiles"];
      delete compact.names;
    }
    records.push({
      id: `${component}-${records.length}`,
      component,
      selector,
      source,
      format,
      attribute,
      identity: clone(identity ?? null),
      scope: clone(e.scope),
      context: compact,
    });
  };
  if (component === "overview") {
    const keys = e.presentation?.primary_keys,
      primary = Array.isArray(keys)
        ? keys
            .map((k) => e.metrics.findIndex((m) => m.key === k))
            .filter((i) => i >= 0)
        : e.metrics.map((_, i) => i),
      extra = e.metrics.map((_, i) => i).filter((i) => !primary.includes(i));
    [primary, extra].forEach((indices, group) =>
      indices.forEach((i, ri) => {
        const m = e.metrics[i],
          p = base.concat("metrics", i),
          c = { metric: m, metricIndex: i, metricKey: m.key };
        const s = `${group ? "[data-additional-metrics] " : ":scope > "}.table-scroll:has(> table.compact-metrics) tbody tr:nth-child(${ri + 1})`;
        add(s, p.concat("key"), "text", c, "data-metric-key");
        add(
          `${s} > th > ${m.key === "ts_pct" ? "button" : "abbr"}`,
          p.concat("key"),
          "abbreviation",
          c,
        );
        add(`${s} > td:nth-child(2)`, p.concat("value"), "overview-value", c);
        add(
          `${s} > td:nth-child(2)`,
          p.concat("value"),
          "overview-value-aria",
          c,
          "aria-label",
        );
        const change = m.sample ? "display_change" : "change";
        if (Object.hasOwn(m, change)) {
          add(`${s} > td:nth-child(4)`, p.concat(change), "change", c);
          add(
            `${s} > td:nth-child(4)`,
            p.concat(change),
            "change-aria",
            c,
            "aria-label",
          );
        }
        if (finite(m.percentile) && m.sample?.status !== "limited") {
          add(
            `${s} meter`,
            p.concat("percentile"),
            "overview-meter",
            c,
            "value",
          );
          add(
            `${s} .percentile-cell > span`,
            p.concat("percentile"),
            "ordinal",
            c,
          );
        }
      }),
    );
    add(":scope > .metric-comparison", base.concat("scope"), "overview-scope");
  }
  if (component === "comparison") {
    add(":scope > .metric-comparison", base.concat("scope"), "scope");
    names.forEach((_, i) =>
      add(
        `.comparison-table thead tr:first-child th:nth-child(${i === 0 ? 1 : 3})`,
        ["player_profiles", i, "player", "player_name"],
        "text",
        { side: i },
      ),
    );
    e.metrics.forEach((m, i) => {
      const p = base.concat("metrics", i),
        r = `.comparison-table tbody tr:nth-child(${i + 1})`;
      add(`${r} > th`, p.concat("label"), "text", {
        metricIndex: i,
        metricKey: m.key,
      });
      ["left", "right"].forEach((side, j) => {
        const c = {
          metric: m[side],
          metricKey: m.key,
          metricIndex: i,
          side,
          player: names[j],
        };
        add(
          `${r} > td:nth-child(${j === 0 ? 1 : 4})`,
          p.concat(side, "value"),
          "comparison-value",
          c,
        );
        if (finite(m[side].percentile)) {
          add(
            `${r} > td:nth-child(${j === 0 ? 2 : 5}) meter`,
            p.concat(side, "percentile"),
            "meter",
            c,
            "value",
          );
          add(
            `${r} > td:nth-child(${j === 0 ? 2 : 5}) .percentile-cell > span`,
            p.concat(side, "percentile"),
            "ordinal",
            c,
          );
        }
      });
      add(`${r} > td:last-child`, p.concat("delta"), "comparison-edge", {
        metric: m,
        names,
        metricIndex: i,
        metricKey: m.key,
      });
    });
    (e.comparison_sections || []).forEach((section, si) =>
      section.metrics.forEach((m, i) => {
        const p = base.concat("comparison_sections", si, "metrics", i),
          r = `[data-comparison-section-index="${si}"] tbody tr:nth-child(${i + 1})`;
        add(`${r} > th`, p.concat("label"), "detail-label", {
          unit: m.unit,
          metricKey: m.key,
        });
        ["left", "right"].forEach((side, j) =>
          add(
            `${r} > td:nth-child(${j + 2})`,
            p.concat(side, "value"),
            "detail-value",
            { metric: m[side], unit: m.unit, metricKey: m.key, side },
          ),
        );
      }),
    );
    let shown = 0;
    (e.impact || []).forEach((m, i) => {
      if (!finite(m.left) || !finite(m.right)) return;
      shown++;
      ["left", "right"].forEach((side, j) =>
        add(
          `.impact-table tbody tr:nth-child(${shown}) > td:nth-child(${j + 2})`,
          base.concat("impact", i, side),
          "impact",
          { key: m.key, side },
        ),
      );
    });
  }
  if (component === "comparison-chart") {
    const i = Math.max(
        0,
        e.metrics.findIndex((m) => m.key === options.key),
      ),
      m = e.metrics[i],
      p = base.concat("metrics", i),
      available = [m.left, m.right].map((v) =>
        v.missing_component_games ? null : v.value,
      ),
      max = Math.max(1, ...available.filter(finite)) * 1.15,
      c = {
        metric: m,
        names,
        available,
        max,
        metricIndex: i,
        metricKey: m.key,
      };
    add(".chart-header h2", p.concat("label"), "chart-title", c);
    add(".chart-header .meta", base.concat("scope"), "scope");
    add(".comparison-bars", p, "chart-aria", c, "aria-label");
    ["left", "right"].forEach((side, j) => {
      const r = `.comparison-bar-row:nth-child(${j + 1})`;
      add(
        `${r} > span`,
        ["player_profiles", j, "player", "player_name"],
        "text",
        { side },
      );
      add(`${r} > strong`, p.concat(side), "chart-value", { ...c, side });
      if (finite(available[j]))
        add(
          `${r} .comparison-bar`,
          p.concat(side, "value"),
          "chart-width",
          { ...c, side },
          "style.width",
        );
    });
    [0, 0.5, 1].forEach((factor, j) =>
      add(`.comparison-bar-axis > span:nth-child(${j + 1})`, p, "chart-axis", {
        ...c,
        factor,
      }),
    );
  }
  if (component === "drawer") {
    const i = e.metrics.findIndex((m) => m.key === options.key);
    if (i < 0) throw Error("Unknown drawer metric");
    const m = e.metrics[i],
      view = options.view || "among_players",
      p = base.concat("metrics", i),
      d = m.distribution?.[view],
      s = d?.summary;
    if (!s) return [];
    const c = {
      metric: m,
      metricIndex: i,
      metricKey: m.key,
      view,
      summary: s,
      name: payload.player_profile?.player?.player_name || "The player",
    };
    add(".drawer-header h2", p.concat("label"), "drawer-title", c);
    add(".drawer-scope", base.concat("scope"), "drawer-scope", c);
    add(".drawer-value strong", p.concat("value"), "drawer-value", c);
    if (Object.hasOwn(m, "percentile"))
      add(".drawer-value > span", p.concat("percentile"), "drawer-rank", c);
    for (const key of ["q1", "q3", "median", "min", "max"])
      if (Object.hasOwn(s, key) && s[key] !== null && !finite(s[key]))
        throw Error("Malformed canonical distribution summary");
    const dp = p.concat("distribution", view, "summary");
    if (Object.hasOwn(s, "n") && s.n !== null)
      add(
        ".drawer-stats > div:nth-child(1) dd",
        dp.concat("n"),
        "drawer-count",
        c,
      );
    if (Object.hasOwn(s, "median"))
      add(
        ".drawer-stats > div:nth-child(2) dd",
        dp.concat("median"),
        "drawer-value",
        c,
      );
    if (Object.hasOwn(s, "q1") && Object.hasOwn(s, "q3"))
      add(
        ".drawer-stats > div:nth-child(3) dd",
        dp.concat("q1"),
        "drawer-quartiles",
        c,
      );
    if (
      view === "across_games" &&
      ["observed_games", "missing_games", "zero_denominator_games"].every(
        (k) => finite(d[k]) && d[k] >= 0,
      ) &&
      (!d.points_truncated ||
        (finite(d.displayed_games) && d.displayed_games >= 0))
    )
      add(".drawer-note", p.concat("distribution", view), "drawer-sample", c);
    else if (
      view !== "across_games" &&
      !m.distribution?.among_players?.focal_in_cohort
    )
      add(".drawer-warning", p, "drawer-qualification", c);
    const bins = (d.bins || [])
      .map((b, i) => ({ b, i }))
      .filter(
        ({ b }) => [b.start, b.end, b.count].every(finite) && b.count >= 0,
      );
    if (s.n > 0) {
      const max = Math.max(1, ...bins.map(({ b }) => b.count)),
        marker = view === "among_players" ? m.value : s.median;
      let lo = Math.min(
          bins[0]?.b.start ?? s.min,
          finite(marker) ? marker : s.min,
        ),
        hi = Math.max(
          bins.at(-1)?.b.end ?? s.max,
          finite(marker) ? marker : s.max,
        );
      if (hi === lo) {
        lo -= 0.5;
        hi += 0.5;
      }
      const geometry = { ...c, lo, hi, max };
      add(
        ".distribution-chart",
        p.concat("label"),
        "drawer-chart-label",
        c,
        "aria-label",
      );
      ["count", "lo", "hi"].forEach((coordinate, axis) =>
        add(
          `.distribution-chart > text:nth-of-type(${axis + 1})`,
          dp,
          "drawer-axis",
          { ...geometry, coordinate },
        ),
      );
      if (finite(marker)) {
        const mp =
          view === "among_players" ? p.concat("value") : dp.concat("median");
        ["x1", "x2"].forEach((attribute) =>
          add(
            ".distribution-chart > line:last-of-type",
            mp,
            "drawer-position",
            geometry,
            attribute,
          ),
        );
      }
      if (view === "across_games") {
        if (finite(s.q1) && finite(s.q3)) {
          add(
            ".distribution-chart > rect:first-of-type",
            dp.concat("q1"),
            "drawer-position",
            geometry,
            "x",
          );
          add(
            ".distribution-chart > rect:first-of-type",
            dp.concat("q1"),
            "drawer-position",
            { ...geometry, q3: s.q3, coordinate: "width" },
            "width",
          );
        }
        const points = (d.points || [])
          .map((point, index) => ({ point, index }))
          .filter(({ point }) => finite(point.value));
        points.forEach(({ point, index }, shown) => {
          const pp = p.concat("distribution", view, "points", index),
            selector = `.distribution-chart > circle:nth-of-type(${shown + 1})`;
          add(selector, pp.concat("value"), "drawer-position", geometry, "cx");
          add(
            selector,
            pp,
            "point-index",
            { ...c, index: shown },
            "data-game-index",
          );
          add(
            `[data-game-select] > option:nth-child(${shown + 2})`,
            pp,
            "point-index",
            { ...c, index: shown },
            "value",
          );
          add(
            selector,
            pp.concat("value"),
            "drawer-point-y",
            { ...c, index: shown },
            "cy",
          );
          add(
            selector,
            pp,
            "drawer-point-label",
            { ...c, separator: "aria" },
            "aria-label",
          );
          add(`${selector} > title`, pp, "drawer-point-label", {
            ...c,
            separator: "title",
          });
          add(
            `[data-game-select] > option:nth-child(${shown + 2})`,
            pp,
            "drawer-option",
            c,
          );
          if (options.selectedPoint === shown)
            add("[data-game-point-info]", pp, "drawer-point-label", {
              ...c,
              separator: "aria",
            });
        });
      }
      bins.forEach(({ b, i }, bi) => {
        const bp = p.concat("distribution", view, "bins", i),
          sel = `.distribution-chart > rect:nth-of-type(${bi + 1 + (view === "across_games" && finite(s.q1) && finite(s.q3) ? 1 : 0)})`;
        add(`${sel} > title`, bp, "bin-title", c);
        ["x", "y", "width", "height"].forEach((coordinate) =>
          add(
            sel,
            bp,
            "bin-geometry",
            { ...c, lo, hi, max, coordinate },
            coordinate,
          ),
        );
      });
    }
  }
  if (component === "overview") {
    let displayed = 0;
    e.metrics.forEach((m, i) => {
      const p = m.peer_comparison;
      if (!p?.mode || p.mode === "withheld") return;
      displayed++;
      const pp = base.concat("metrics", i, "peer_comparison"),
        r = `[data-peer-details] [data-peer-index="${displayed - 1}"]`,
        c = { metricKey: m.key, label: m.label };
      add(`${r} > h4`, pp, "peer-heading", c);
      add(`${r} > p:not(.meta)`, pp, "peer-standing", c);
      add(`${r} > .meta`, pp, "peer-sample", c);
      const bins = p.distribution?.among_players?.bins || [],
        max = Math.max(1, ...bins.map((b) => b.count));
      bins.forEach((b, bi) => {
        const bp = pp.concat("distribution", "among_players", "bins", bi),
          rect = `${r} svg > rect:nth-of-type(${bi + 1})`;
        add(`${rect} > title`, bp, "peer-bin");
        ["x", "y", "width", "height"].forEach((coordinate) =>
          add(
            rect,
            bp,
            "peer-bin-geometry",
            { index: bi, length: bins.length, max, coordinate },
            coordinate,
          ),
        );
      });
    });
    const vi = e.insights?.findIndex((v) => v.game_variation),
      v = vi >= 0 ? e.insights[vi].game_variation : null;
    if (v) {
      const mi = e.metrics.findIndex((m) => m.key === v.metric_key),
        m = e.metrics[mi],
        games = m?.distribution?.across_games,
        stats = v.summary;
      if (
        games?.points?.some(
          (p) => finite(p.value) && Number.isFinite(Date.parse(p.date)),
        ) &&
        stats &&
        [stats.min, stats.max, stats.median, stats.q1, stats.q3].every(finite)
      ) {
        const sp = base.concat("insights", vi, "game_variation", "summary"),
          r = "[data-game-variation]",
          c = { min: stats.min, max: stats.max, q3: stats.q3 };
        const points = games.points
            .map((point, index) => ({ point, index }))
            .filter(
              ({ point }) =>
                finite(point.value) && Number.isFinite(Date.parse(point.date)),
            ),
          times = points.map(({ point }) => Date.parse(point.date)),
          low = Math.min(...times),
          high = Math.max(...times);
        points.forEach(({ point, index }, shown) => {
          const pp = base.concat(
              "metrics",
              mi,
              "distribution",
              "across_games",
              "points",
              index,
            ),
            sel = `${r} svg > circle:nth-of-type(${shown + 1})`,
            context = { ...c, low, high, metricKey: m.key, label: m.label };
          add(`${sel} > title`, pp, "variation-point-label", context);
          add(sel, pp.concat("date"), "variation-point-x", context, "cx");
          add(
            sel,
            pp.concat("value"),
            "variation-geometry",
            { ...context, coordinate: "y" },
            "cy",
          );
          add(sel, pp.concat("value"), "variation-point-radius", context, "r");
          add(
            sel,
            pp.concat("value"),
            "variation-point-color",
            context,
            "fill",
          );
        });
        [0, points.length - 1].forEach((shown, label) =>
          add(
            `${r} svg > text:nth-of-type(${label + 1})`,
            base.concat(
              "metrics",
              mi,
              "distribution",
              "across_games",
              "points",
              points[shown].index,
              "date",
            ),
            "text",
          ),
        );
        add(`${r} svg > text:nth-of-type(3)`, sp.concat("max"), "text");
        add(`${r} svg > text:nth-of-type(4)`, sp.concat("min"), "text");
        add(
          `${r} svg > rect`,
          sp.concat("q3"),
          "variation-geometry",
          { ...c, coordinate: "y" },
          "y",
        );
        add(
          `${r} svg > rect`,
          sp.concat("q1"),
          "variation-geometry",
          { ...c, coordinate: "height" },
          "height",
        );
        ["y1", "y2"].forEach((attr) =>
          add(
            `${r} svg > line`,
            sp.concat("median"),
            "variation-geometry",
            { ...c, coordinate: attr },
            attr,
          ),
        );
        add(`${r} > .meta`, sp.concat("n"), "variation-count", {
          truncated: !!games.points_truncated,
          displayed: games.displayed_games,
        });
      }
    }
  }
  return records;
}
export function expectedClaim(payload, record) {
  const c = {
    ...record.context,
    evidence: { scope: payload.semantic_evidence.scope },
  };
  if (c.metricPath) c.metric = claimSource(payload, c.metricPath);
  if (c.summaryPath) c.summary = claimSource(payload, c.summaryPath);
  if (c.namesPath)
    c.names = claimSource(payload, c.namesPath).map(
      (p) => p.player.player_name,
    );
  return formatClaim(claimSource(payload, record.source), record.format, c);
}
// One pass over targets; duplicate detection must not scan all IDs for each claim.
export function claimTargetCounts(ids) {
  const counts = new Map();
  for (const id of ids) counts.set(id, (counts.get(id) || 0) + 1);
  return counts;
}
export function validateRenderedClaims(root, payload, session) {
  const errors = [];
  let schema = [];
  try {
    assertFiniteTree(session.evidence);
    schema = claimSchema(session.evidence, session.component, session.options);
  } catch (error) {
    errors.push({ reason: error.message });
  }
  if (!equal(schema, session.descriptors))
    errors.push({ reason: "Descriptor schema/source/context changed" });
  if (
    !equal(session.evidence.semantic_evidence, payload.semantic_evidence) ||
    !equal(session.evidence.player_profiles, payload.player_profiles) ||
    !equal(session.evidence.player_profile, payload.player_profile)
  )
    errors.push({ reason: "Canonical identity/metric/scope/source changed" });
  for (const r of schema) {
    try {
      const nodes = root.querySelectorAll(r.selector);
      if (nodes.length !== 1)
        throw Error(`Expected one destination, found ${nodes.length}`);
      const node = nodes[0],
        actual =
          r.attribute === "style.width"
            ? node.style.width
            : r.attribute
              ? node.getAttribute(r.attribute)
              : node.textContent,
        value = expectedClaim(session.evidence, r);
      const match =
        r.attribute === "style.width"
          ? /^-?[\d.]+%$/.test(actual) &&
            Math.abs(parseFloat(actual) - parseFloat(value)) < 1e-4
          : actual === value;
      if (!match)
        throw Error("Rendered destination contradicts canonical evidence");
    } catch (error) {
      errors.push({
        id: r.id,
        selector: r.selector,
        source: r.source,
        reason: error.message,
      });
    }
  }
  const ids = [...root.querySelectorAll("[data-ui-claim]")].flatMap((n) =>
      n.getAttribute("data-ui-claim").split(" "),
    ),
    wanted = new Set(schema.map((r) => r.id));
  const counts = claimTargetCounts(ids);
  for (const id of counts.keys())
    if (!wanted.has(id))
      errors.push({ id, reason: "Unexpected claim destination" });
  for (const id of wanted)
    if (counts.get(id) !== 1)
      errors.push({ id, reason: "Missing or duplicate claim destination" });
  return {
    version: UI_CLAIM_VERSION,
    status: errors.length ? "invalid" : "incomplete",
    checked: schema.length,
    errors,
    gaps:
      UI_CLAIM_GAPS[
        session.component.startsWith("comparison")
          ? "comparison"
          : session.component
      ] || UI_CLAIM_GAPS.legacy,
  };
}
export function bindRenderedClaims(
  root,
  payload,
  component,
  options = {},
  evidence = prepareClaimEvidence(payload),
) {
  let descriptors = [];
  try {
    descriptors = claimSchema(evidence, component, options);
  } catch {
    /* Validation retains the construction error and withholds this surface. */
  }
  const session = { component, options: clone(options), evidence, descriptors };
  root
    .querySelectorAll("[data-ui-claim]")
    .forEach((n) => n.removeAttribute("data-ui-claim"));
  for (const r of session.descriptors) {
    const nodes = root.querySelectorAll(r.selector);
    if (nodes.length === 1)
      nodes[0].setAttribute(
        "data-ui-claim",
        [nodes[0].getAttribute("data-ui-claim"), r.id]
          .filter(Boolean)
          .join(" "),
      );
  }
  root.uiClaimSession = session;
  return enforceRenderedClaims(root, payload, session);
}
export function enforceRenderedClaims(
  root,
  payload,
  session = root.uiClaimSession,
) {
  const report = validateRenderedClaims(root, payload, session);
  root.uiClaimReport = report;
  if (report.status === "invalid") {
    const warning = root.ownerDocument.createElement("p");
    warning.className = "meta";
    // Local-only bounded diagnostics: no values, identities, source paths or telemetry.
    const counts = (root.ownerDocument.uiClaimFailureCounts ||= {});
    const code = report.errors.some(
      (e) => e.reason === "Canonical identity/metric/scope/source changed",
    )
      ? "source_changed"
      : report.errors.some(
            (e) =>
              e.reason ===
              "Rendered destination contradicts canonical evidence",
          )
        ? "destination_mismatch"
        : "binding_structure";
    counts[code] = Math.min(1000000, (counts[code] || 0) + 1);
    warning.dataset.claimFailureCode = code;
    warning.textContent =
      "This factual view could not be verified. Refresh the page to reload its evidence. If this continues, report code: " +
      code +
      ".";
    root.replaceChildren(warning);
    if (root.tagName === "DIALOG") {
      const close = root.ownerDocument.createElement("button");
      close.type = "button";
      close.textContent = "Close metric details";
      close.setAttribute("data-drawer-close", "");
      close.addEventListener("click", () => root.close());
      root.append(close);
      close.focus();
    }
  }
  return report;
}
