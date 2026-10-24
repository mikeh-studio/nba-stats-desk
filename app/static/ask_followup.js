// Conservative UI navigation aid. Backend identity and evidence checks still apply.
const words = new Set(("rebounding passing rating ratings plus minus pattern patterns clutch bench starter starters threshold limited research review playmaking give overview among guards wings forwards centers s a an the this that these those it its he him his she her they them their both each "
+ "player players report result results answer answers comparison compare compared versus vs and or but of for from to through in on at by with without about against than as per "
+ "is are was were be been do does did can could would will should has have had i me my you your we us please tell show explain expand break down breakdown why how what which when where "
+ "only just also instead now same other another more less most least much many better worse higher lower above below average averages averaged mean median total totals sum rate rates percentage percentages percent "
+ "points point pts rebounds rebound reb assists assist ast steals steal stl blocks block blk turnovers turnover tov minutes minute min scoring score scored scores shooting shot shots made makes missed misses attempts attempted "
+ "field goal goals free throw throws three two pointers pointer ts efg fg ft usage efficiency offensive offense defensive defense production performance perform performed playing play played "
+ "time workload sample size games game appearances appearance count counts distribution trend trends consistent consistency variation variability volatile volatility percentile rank ranked ranking league baseline peers peer group groups teammates teammate team teams impact contribution "
+ "season seasons regular playoffs playoff postseason pre all entire last past previous prior current year years month months week weeks day days date dates window period periods range recent recently since before after during between until over under every first second third fourth half quarter "
+ "january february march april may june july august september october november december jan feb mar apr jun jul aug sep sept oct nov dec today yesterday tonight "
+ "explanation detail details meaning means value values numbers number statistic statistics stat stats evidence source sources coverage available missing qualify qualified qualification enough "
+ "up out off floor court win wins loss losses winning losing home away split splits include including exclude excluding besides except change use keep narrow focus highlight highest lowest best worst peak low high top bottom single start end per36 per100 true effective difference differences gap").split(/\s+/));
const tokens = text => String(text || "").toLowerCase().match(/[\p{L}\p{N}]+/gu) || [];

export function isFollowupReport(payload) {
  return Boolean(payload && (["ok", "clarification_required"].includes(payload.status) ||
    (payload.status == null && payload.availability_scope && payload.availability_evidence)));
}

export function followupFitsReport(question, payload, reportQuestion = "", suggestions = []) {
  if (!payload) return false;
  const normalize = value => String(value || "").trim().replace(/\s+/g, " ").toLowerCase();
  const proposed = normalize(question);
  // Exact suggestions are already scoped by the report, including vocabulary
  // outside this conservative navigation aid. Edits still use the normal gate.
  if (proposed && [...(payload.followups || []), ...suggestions].some(text => normalize(text) === proposed)) return true;
  const profiles = payload.player_profiles || [payload.player_profile || payload.reference_player];
  const players = payload.conversation_context?.players || profiles.map(p => p?.player).filter(Boolean);
  const allowed = new Set([...words, ...tokens(reportQuestion), ...players.flatMap(p => tokens(p.player_name))]);
  const parts = tokens(question);
  return parts.length > 0 && parts.every(word => allowed.has(word) || /^\d+$/.test(word));
}
