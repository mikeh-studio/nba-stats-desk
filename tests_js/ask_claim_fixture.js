// Literal independent fictional observations, shared by Node and real DOM tests.
export function overviewFixture() {
  const stat = (key, label, value, unit = "per game") => ({
    key,
    label,
    unit,
    value,
    percentile: 50,
    change: 1,
    display_change: 1,
    valid_games: 2,
    observed_games: 2,
    missing_component_games: 0,
    cohort_size: 3,
  });
  const points = stat("pts", "Points", 5);
  points.distribution = {
    among_players: {
      focal_in_cohort: true,
      summary: { n: 3, min: 0, max: 10, median: 5, q1: 2.5, q3: 7.5 },
      bins: [
        { start: 0, end: 5, count: 1 },
        { start: 5, end: 10, count: 2 },
      ],
    },
    across_games: {
      summary: { n: 2, min: 0, max: 10, median: 5, q1: 2.5, q3: 7.5 },
      bins: [
        { start: 0, end: 5, count: 1 },
        { start: 5, end: 10, count: 1 },
      ],
      points: [
        { date: "2025-01-01", value: 0 },
        { date: "2025-01-02", value: 10 },
      ],
      observed_games: 2,
      missing_games: 0,
      zero_denominator_games: 0,
    },
  };
  points.peer_comparison = {
    mode: "ordinal",
    kind: "position",
    group: "Guard",
    rank: 2,
    tied: false,
    cohort_size: 10,
    season: "2024-25",
    phases: ["Regular Season"],
    start: "2025-01-01",
    end: "2025-01-02",
    qualification: { min_games: 2 },
    classified_players: 10,
    eligible_players: 12,
    distribution: {
      among_players: {
        bins: [
          { start: 0, end: 5, count: 4 },
          { start: 5, end: 10, count: 6 },
        ],
      },
    },
  };
  return {
    player_profile: { player: { player_id: 101, player_name: "Avery" } },
    semantic_evidence: {
      player_id: 101,
      scope: {
        start: "2025-01-01",
        end: "2025-01-02",
        previous_start: "2024-01-01",
        previous_end: "2024-01-02",
        phases: ["Regular Season"],
      },
      insights: [
        {
          game_variation: {
            metric_key: "pts",
            summary: { n: 2, min: 0, max: 10, median: 5, q1: 2.5, q3: 7.5 },
            spread_text: "Fictional spread",
            peak_text: "Fictional high",
          },
        },
      ],
      metrics: [
        points,
        stat("ast", "Assists", 5),
        stat("fga", "Field-goal attempts", 0),
        stat("ft_pct", "Free throw percentage", null, "percent"),
      ],
      presentation: { primary_keys: ["ast", "pts", "fga"], takeaways: [] },
    },
  };
}
export function comparisonFixture() {
  const p = overviewFixture();
  const side = (value) => ({
    value,
    percentile: 50,
    valid_games: 2,
    observed_games: 2,
    missing_component_games: 0,
    cohort_size: 3,
  });
  return {
    player_profiles: [
      { player: { player_id: 101, player_name: "Avery", games_sampled: 2 } },
      { player: { player_id: 202, player_name: "Blair", games_sampled: 2 } },
    ],
    semantic_evidence: {
      kind: "player_comparison",
      scope: p.semantic_evidence.scope,
      metrics: [
        {
          key: "pts",
          label: "Points",
          left: side(5),
          right: side(5),
          delta: 0,
        },
        {
          key: "ast",
          label: "Assists",
          left: side(2),
          right: side(1),
          delta: 1,
        },
      ],
      min_games: 2,
      paragraphs: [],
      impact: [
        {
          key: "ws48",
          label: "WS/48",
          left: 0.1,
          right: 0.2,
          definition: "Fictional",
        },
      ],
      comparison_sections: [
        {
          key: "shooting",
          title: "Shooting",
          description: "Pooled",
          metrics: [
            {
              key: "fg_pct",
              label: "FG%",
              unit: "percent",
              left: { ...side(50), components: { fga: 8 } },
              right: {
                ...side(25),
                missing_component_games: 1,
                valid_games: 1,
                components: { fga: 4 },
              },
              comparison: "Partial inputs",
            },
          ],
        },
      ],
    },
  };
}
