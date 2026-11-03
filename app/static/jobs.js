const $ = (id) => document.getElementById(id);
const labels = {
  blocked: "Blocked",
  failed: "Failed",
  success: "Succeeded",
  degraded: "Needs attention",
  preflight_passed: "Preflight passed",
  running: "Running",
  interrupted: "Interrupted",
  not_run: "Not run",
};
const reasons = {
  season_rollover_required:
    "The warehouse still holds the previous season. Preserve and validate the 2025–26 archive before enabling current-season refreshes.",
  pipeline_timeout:
    "The pipeline exceeded its execution limit. Its process group was stopped.",
  pipeline_command_failed:
    "A pipeline command failed. Open its redacted log for the underlying error.",
  stale_or_empty_publication:
    "The pipeline exited, but its published data was empty or outside the freshness threshold.",
  worker_not_running:
    "This record did not finish and the worker lock is no longer held. Its result is unknown.",
};
let data = null,
  zone = "America/Los_Angeles",
  selected = null,
  busy = false,
  lastPayload = null;
const text = (id, value) => {
  $(id).textContent = value;
};
function renderMapping(container, mapping, source) {
  container.classList.add("mapping-checks");
  container.replaceChildren();
  container.hidden = source !== "espn";
  if (container.hidden) return;
  container.append(el("h2", "Game-ID mappings"));
  if (mapping?.status === "degraded") container.append(el("p", "Needs attention · source discovery or mapping checks need review.", "failure-copy"));
  if (!mapping || mapping.games == null) {
    container.append(el("p", "Mapping observation unavailable. Refresh the recorded warehouse status to check aliases and discrepancies.", "muted"));
    return;
  }
  container.append(el("p", `${mapping.resolved ?? 0} / ${mapping.games} games mapped · ${mapping.unresolved ?? 0} unresolved · ${mapping.conflict ?? 0} conflicting aliases`));
  if (mapping.reviewed_games != null) container.append(el("p", `PBP Stats checks recorded for ${mapping.reviewed_games} / ${mapping.games} current game revisions.`, "muted"));
  if (mapping.source_status) container.append(el("p", `PBP Stats discovery: ${mapping.source_status.replaceAll("_", " ")}${mapping.source_observed_at ? ` · captured ${stamp(mapping.source_observed_at)}` : ""}`, "muted"));
  container.append(el("p", `Mapping observation: ${stamp(mapping.checked_at)}. Identity links do not certify statistics, PDF checks or league coverage.`, "muted"));
  const count = mapping.discrepancy_count ?? 0;
  container.append(el("h3", `${count} PBP Stats discrepanc${count === 1 ? "y" : "ies"}`));
  for (const row of mapping.discrepancies || []) {
    const item = el("div", null, "observed-item");
    item.append(el("strong", `${row.away || "Away"} at ${row.home || "Home"} · ${day(row.date)}`));
    item.append(el("span", `ESPN ${row.away_score ?? "?"}–${row.home_score ?? "?"} · PBP Stats ${row.provider_away_score ?? "?"}–${row.provider_home_score ?? "?"} (away–home)`));
    item.append(el("span", `ESPN ${row.source_game_id || "unknown"} · NBA ${row.nba_game_id || "unresolved"}`));
    item.append(el("span", row.identity_verified_separately ? "NBA identity verified separately; PBP Stats discrepancy remains." : "Source discrepancy requires review; accepted ESPN facts are retained."));
    container.append(item);
  }
  if (count > (mapping.discrepancies || []).length) container.append(el("p", "Additional discrepancies are retained in the audit ledger.", "muted"));
}
function stamp(value, options = {}) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? "Unavailable"
    : new Intl.DateTimeFormat("en-US", {
        timeZone: zone,
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZoneName: "short",
        ...options,
      }).format(date);
}
function day(value) {
  if (!value) return "Unavailable";
  const d = new Date(`${value.slice(0, 10)}T12:00:00Z`);
  return Number.isNaN(d.valueOf())
    ? "Unavailable"
    : new Intl.DateTimeFormat("en-US", {
        timeZone: "UTC",
        month: "short",
        day: "numeric",
        year: "numeric",
      }).format(d);
}
function duration(value) {
  if (value == null || !Number.isFinite(value)) return "Not recorded";
  if (value < 1) return `${Math.round(value * 1000)} ms`;
  if (value < 60) return `${value.toFixed(1)} sec`;
  const seconds = Math.round(value),
    m = Math.floor(seconds / 60),
    s = seconds % 60;
  return m < 60 ? `${m}m ${s}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}
function runDuration(run) {
  if (run?.status === "running" && run.started_at) {
    const seconds = (Date.now() - new Date(run.started_at).valueOf()) / 1000;
    return Number.isFinite(seconds)
      ? `${duration(Math.max(0, seconds))} elapsed`
      : "Unavailable";
  }
  return duration(run?.duration_seconds);
}
function badge(status) {
  const span = document.createElement("span");
  span.className = `badge ${Object.hasOwn(labels, status) ? status : ""}`;
  span.textContent = labels[status] || "Unavailable";
  return span;
}
function el(tag, content, className) {
  const node = document.createElement(tag);
  if (content != null) node.textContent = content;
  if (className) node.className = className;
  return node;
}
function history() {
  const all = data.history || [],
    filter = $("history-filter").value;
  const rows = all.filter(
    (r) =>
      filter === "all" ||
      (filter === "attention"
        ? ["blocked", "failed", "interrupted"].includes(r.status) || r.game_mapping?.status === "degraded"
        : r.kind === filter),
  );
  text("history-count", all.length);
  $("history-rows").replaceChildren();
  $("history-empty").hidden = rows.length > 0;
  $("table-wrap").hidden = !rows.length;
  $("table-hint").hidden = !rows.length;
  $("history-empty").querySelector("h3").textContent = all.length
    ? "No matching records"
    : "No run records yet";
  $("history-empty").querySelector("p").textContent = all.length
    ? "Choose another filter to see recorded checks and refreshes."
    : "New checks and refreshes will appear here. Existing status is shown above; earlier durations are not reconstructed.";
  for (const r of rows) {
    const tr = document.createElement("tr"),
      time = el("td", stamp(r.started_at));
    const type = el("td", r.kind === "preflight" ? "Preflight" : "Refresh"),
      status = el("td");
    status.append(badge(r.status));
    if (r.game_mapping?.status === "degraded") status.append(badge("degraded"));
    if (r.reason)
      status.append(
        el(
          "span",
          r.reason === "season_rollover_required"
            ? "Season rollover required"
            : r.reason.replaceAll("_", " "),
          "time-sub",
        ),
      );
    const details = el("td"),
      button = el("button", "View", "detail-button");
    button.type = "button";
    button.setAttribute(
      "aria-label",
      `View ${r.kind} from ${stamp(r.started_at)}`,
    );
    button.addEventListener("click", () => openRun(r));
    details.append(button);
    tr.append(time, type, status, el("td", runDuration(r)), details);
    $("history-rows").append(tr);
  }
}
function render(payload) {
  lastPayload = payload;
  const jobs = payload.jobs || [payload.job];
  const chosen = $("job-select").value;
  data = jobs.find((job) => job.id === chosen) || payload.job;
  $("job-picker").hidden = jobs.length < 2;
  $("job-select").replaceChildren(
    ...jobs.map((job) => {
      const option = el("option", job.name);
      option.value = job.id;
      return option;
    }),
  );
  $("job-select").value = data.id;
  text("job-description", data.description || "Core NBA refresh");
  zone = data.schedule?.timezone || "America/Los_Angeles";
  text(
    "read-time",
    `Checked ${stamp(payload.checked_at, { month: undefined, day: undefined })}`,
  );
  text("job-name", data.name);
  text("job-season", data.season || "Not configured");
  text("fresh-season", data.season ? `· ${data.season}` : "");
  text("result", labels[data.status] || "Unavailable");
  text(
    "result-note",
    data.history[0]?.status === "running"
      ? `Started ${stamp(data.history[0].started_at)}`
      : data.last_checked_at
        ? stamp(data.last_checked_at)
        : "No check recorded",
  );
  text(
    "success",
    data.last_success_at
      ? stamp(data.last_success_at, {
          hour: undefined,
          minute: undefined,
          timeZoneName: undefined,
        })
      : "Not yet",
  );
  text(
    "success-note",
    data.last_success_at
      ? "Last successful refresh"
      : "No successful refresh recorded",
  );
  const running =
    data.history[0]?.status === "running" ? data.history[0] : null;
  text(
    "duration-label",
    running ? "CURRENT RUN ELAPSED" : "LAST RECORDED DURATION",
  );
  text(
    "duration",
    running
      ? runDuration(running)
      : data.last_duration_seconds == null
        ? "—"
        : duration(data.last_duration_seconds),
  );
  text(
    "duration-note",
    running
      ? "Running · elapsed wall time"
      : data.history[0]?.kind === "preflight"
        ? "Preflight check · no ingestion"
        : data.history.length
          ? "Latest recorded execution"
          : "Earlier duration unavailable",
  );
  text(
    "freshness",
    data.freshness.data_through
      ? day(data.freshness.data_through)
      : "Unavailable",
  );
  text(
    "freshness-note",
    data.freshness.data_through
      ? `${data.freshness.age_days ?? "Unknown"} ${data.freshness.age_days === 1 ? "day" : "days"} old · ${data.freshness.state === "within_threshold" ? "within 10-day limit" : "outside freshness limit"}`
      : `No ${data.season || "target-season"} publication observed`,
  );
  text("enabled", data.scheduler.label);
  text(
    "attempts",
    data.attempt_count > 3
      ? `${data.attempt_count} attempts · includes manual runs`
      : `${data.attempt_count} / 3 refresh attempts`,
  );
  const weekdays = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
  ];
  const plan = data.schedule;
  text(
    "cadence",
    plan
      ? `${weekdays[plan.weekday]} · ${plan.hour > 12 ? plan.hour - 12 : plan.hour || 12}:00 ${plan.hour >= 12 ? "p.m." : "a.m."}`
      : "Not configured",
  );
  const attention = ["blocked", "failed", "interrupted"].includes(data.status);
  const activationUnknown = data.scheduler.loaded === null;
  $("job-notice").hidden = !attention && !!data.scheduler.loaded;
  if (attention || !data.scheduler.loaded) {
    text(
      "notice-title",
      attention
        ? `Refresh ${labels[data.status].toLowerCase()}`
        : activationUnknown
          ? "Scheduler status unavailable"
          : "Weekly schedule is prepared",
    );
    text(
      "notice-copy",
      reasons[data.reason] ||
        (data.scheduler.loaded
          ? "Open the latest run for details."
          : activationUnknown
            ? "Could not verify whether the weekly job is enabled. The planned time below is not a confirmed scheduled run."
            : "The weekly job has not been enabled. The planned time below is not an active scheduled run."),
    );
    $("notice-badge").replaceChildren(badge(data.status));
  }
  text(
    "next-heading",
    data.scheduler.loaded ? "Next eligible run" : "Next planned slot",
  );
  const next = plan?.next_scheduled_run || plan?.next_weekly_at;
  text(
    "next-date",
    plan
      ? stamp(next, {
          weekday: "short",
          hour: undefined,
          minute: undefined,
          timeZoneName: undefined,
        })
      : "Not configured",
  );
  text(
    "next-time",
    plan ? stamp(next, { month: undefined, day: undefined }) : "",
  );
  text("timezone", zone);
  text(
    "schedule-note",
    data.scheduler.loaded
      ? "Enabled. The Mac must be awake, logged in and online."
      : activationUnknown
        ? "Activation unknown · this is a planned time only."
        : "Not enabled · this time is a plan, not a queued run.",
  );
  $("retry-note").hidden = !plan?.retry_at;
  if (plan?.retry_at)
    text(
      "retry-note",
      `Next eligible attempt: ${stamp(plan.retry_at)}. Local due checks run hourly.`,
    );
  const observed = $("observed-coverage");
  observed.replaceChildren();
  if (!data.freshness.observed.length)
    observed.append(el("p", "No warehouse observation recorded yet.", "muted"));
  for (const g of data.freshness.observed) {
    const item = el("div", null, "observed-item");
    item.append(
      el(
        "strong",
        `${g.season || "Unknown season"} · ${Number(g.row_count || 0).toLocaleString()} appearances`,
      ),
      el(
        "span",
        `Through ${day(g.data_through)}${g.season !== data.season ? " · previous season" : ""}`,
      ),
    );
    if (data.source === "espn" && g.accepted_games != null)
      item.append(
        el("span", ` · ${g.accepted_games} accepted boxes · observed sample`),
      );
    observed.append(item);
  }
  text(
    "observed-at",
    data.freshness.observed_at
      ? `Observation recorded ${stamp(data.freshness.observed_at)}.`
      : "No warehouse check has been recorded.",
  );
  const checks = data.coverage_checks;
  if (data.source === "espn") {
    const item = el("div", null, "observed-item coverage-checks");
    item.append(el("strong", "ESPN coverage checks"));
    if (checks?.dates_expected != null) {
      item.append(el("span", `${checks.dates_checked} / ${checks.dates_expected} dates checked · ${checks.accepted_games} / ${checks.expected_final_games} listed finals accepted`));
      item.append(el("span", `Games checked through ${day(checks.games_checked_through)} · ${checks.status}`));
      const p = checks.player_checks || {};
      item.append(el("span", `${p.unresolved_rows ?? 0} unresolved · ${p.dropped_rows ?? 0} dropped · ${p.duplicate_rows ?? 0} duplicate appearances`));
      if (checks.missing_dates?.length) item.append(el("span", `Missing dates: ${checks.missing_dates.join(", ")}`));
      if (checks.missing_games?.length) item.append(el("span", `Missing games: ${checks.missing_games.join(", ")}`));
      const blocked = Object.entries(checks.metric_checks || {}).filter(([, value]) => !value.coverage_ready);
      item.append(el("span", checks.status !== "passed" ? `All metrics blocked by game/player coverage: ${(checks.issues || []).map(code => code.replaceAll("_", " ")).join(", ")}` : blocked.length ? `Metric coverage blockers: ${blocked.map(([key, value]) => `${key.toUpperCase().replace("_PER36", "/36")} (${value.missing_input_rows} missing input${value.missing_input_rows === 1 ? "" : "s"})`).join(", ")}` : "Metric coverage prerequisites passed."));
      item.append(el("span", "ESPN-reported coverage. Ranking source and qualification guards remain in force."));
    } else item.append(el("span", "Not recorded yet. The next refresh will report date, game, player and metric checks."));
    observed.append(item);
  }
  renderMapping($("game-mapping"), data.game_mapping, data.source);
  $("record-issues").hidden = !data.issues.length;
  text("record-issues", data.issues.join(" "));
  history();
}
function openRun(run) {
  selected = run;
  text(
    "run-title",
    run.kind === "preflight"
      ? "Preflight check"
      : run.source === "espn"
        ? "ESPN box-score refresh"
        : "Weekly NBA refresh",
  );
  const container = $("run-details");
  container.replaceChildren(badge(run.status));
  const grid = el("div", null, "detail-grid");
  for (const [label, value] of [
    ["Started", stamp(run.started_at)],
    ["Finished", stamp(run.finished_at)],
    [run.status === "running" ? "Elapsed" : "Duration", runDuration(run)],
    ["Season", run.season || "Not recorded"],
  ]) {
    const pair = el("div");
    pair.append(el("span", label), el("strong", value));
    grid.append(pair);
  }
  container.append(grid);
  const mapping = el("section", null, "observed-item");
  renderMapping(mapping, run.game_mapping, run.source);
  container.append(mapping);
  if (run.reason)
    container.append(
      el(
        "p",
        reasons[run.reason] ||
          `Recorded failure: ${run.reason.replaceAll("_", " ")}.`,
        "failure-copy",
      ),
    );
  if (run.stages?.length) {
    container.append(el("h3", "Execution stages"));
    const list = el("ul", null, "stages");
    for (const s of run.stages) {
      const li = el("li");
      li.append(
        el("span", s.name),
        el(
          "span",
          `${labels[s.status] || s.status} · ${duration(s.duration_seconds)}`,
        ),
      );
      list.append(li);
    }
    container.append(list);
  } else
    container.append(
      el(
        "p",
        run.kind === "preflight"
          ? "This check did not start ingestion or change warehouse data."
          : "No pipeline commands were recorded.",
        "muted",
      ),
    );
  $("load-log").hidden = !run.has_log;
  $("load-log").disabled = false;
  $("log-output").hidden = true;
  text("log-output", "");
  text(
    "log-note",
    run.has_log
      ? "Last 64 KB · secrets redacted"
      : "No command log for this record",
  );
  if (!$("run-dialog").open) $("run-dialog").showModal();
}
async function load() {
  if (busy) return;
  busy = true;
  $("refresh").disabled = true;
  try {
    const response = await fetch("/api/jobs", { cache: "no-store" });
    if (!response.ok) throw new Error("Read failed");
    render(await response.json());
    $("read-error").hidden = true;
  } catch {
    $("read-error").hidden = false;
    text(
      "read-error",
      "Could not read current job records. Any values shown are from the last successful page update.",
    );
  } finally {
    busy = false;
    $("refresh").disabled = false;
  }
}
$("refresh").addEventListener("click", load);
$("job-select").addEventListener("change", () => {
  if (lastPayload) render(lastPayload);
});
$("history-filter").addEventListener("change", () => {
  if (data) history();
});
$("close-dialog").addEventListener("click", () => $("run-dialog").close());
$("load-log").addEventListener("click", async () => {
  const run = selected;
  $("load-log").disabled = true;
  try {
    const response = await fetch(`/api/jobs/runs/${run.id}/log`);
    if (!response.ok) throw new Error();
    const log = await response.json();
    if (selected !== run) return;
    text("log-output", log.text || "(Empty command log)");
    $("log-output").hidden = false;
    text(
      "log-note",
      log.truncated
        ? "Showing the last 64 KB · redacted"
        : "Command log · redacted",
    );
  } catch {
    text("log-note", "The command log is unavailable.");
  } finally {
    $("load-log").disabled = false;
  }
});
await load();
setInterval(() => {
  if (!document.hidden) load();
}, 15000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) load();
});
