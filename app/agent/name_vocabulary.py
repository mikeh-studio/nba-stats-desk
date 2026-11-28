"""Ask vocabulary that takes precedence over generated names in question text."""

# Exclude question/scope words so prose cannot become a player-name candidate.
CONTEXT_WORDS = frozenset(
    "how did does do has have had is was were he she his her they their it its "
    "play played plays playing perform performed performing performance doing done "
    "participate participates participated participating participation "
    "reach reaches reached reaching make makes making appear appears appearing "
    "compare compares compared comparing with to and vs versus than who better "
    "last this current previous prior season seasons regular playoffs playoff "
    "postseason year years games game show me tell about points rebounds assists "
    "steals blocks stats statistics average averaged per in on at of the a an "
    "for from through between during since until please overview summary "
    "can could would you give review an analysis of among guards wings centers forwards "
    "scoring rebounding playmaking shooting efficiency fare been assist rebound steal block "
    "three threes leaders leader christmas day".split()
)

# Task vocabulary bounds name spans. Unknown prose remains for the route's scope
# validator; this is not a general named-entity model or a permissive query parser.
SCOPE_WORDS = CONTEXT_WORDS | frozenset(
    """
what which where when why are am will should let us we our your get find count
many much number appearances appeared appearance log logs totals total rate rates
percent percentage percentile percentiles ranking rankings rank ranked top bottom
league nba baseline peer peers similar similarity most least best worst median
career split splits toughest hardest easiest struggle struggled matchup matchups
home away road wins losses win loss won lost against opponent
opponents teammate teammates without out absent absence missed miss sat sitting
sidelined injury injured unavailable availability both together also only include
exclude including excluding while not no yes same different higher lower more less
shooting true effective field goal goals free throw throws three two made makes
attempts attempted turnovers turnover minutes scoring rebounding usage efficiency
point boards foul fouls plusminus gp pts reb rbs oreb dreb pf ast stl blk blcks tov tos
min per36 ppg rpg apg mpg fg fga fgm ft fta ftm fg3a fg3m 3pm 3p 3pt pct ts efg bsi
plus minus stat line
breakdown descriptive research study studies adjusted unadjusted estimate impact
effect significant significance statistically statistical reliable reliability
uncertainty confidence interval causal cause pattern difference distribution
fantasy score weighted proxy summarize analyze break down before after as improved improve trade
over like resembles alike nearest trending beside besides other playmaking leads measure
trend trends chart table plot rolling recent previous past next first second third
fourth five ten all each every minimum maximum at least qualified eligible players
january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday today yesterday tonight
mvp rookie award awards defensive finals winner winners year defensive offense
basketball regular post season postseason round conference eastern western cup
boston celtics brooklyn nets new york knicks philadelphia sixers toronto raptors
chicago bulls cleveland cavaliers detroit pistons indiana pacers milwaukee bucks
atlanta hawks charlotte hornets miami heat orlando magic washington wizards
denver nuggets minnesota timberwolves oklahoma city thunder portland trail blazers
utah jazz golden state warriors los angeles clippers lakers phoenix suns sacramento
kings dallas mavericks houston rockets memphis grizzlies orleans pelicans san antonio
spurs selected clarification player use using compared played playing performing
box scores there any game where scored blocked shots dunks dunk layups layup jumpers
heaves putbacks floaters hooks corner catch pullup pullups
""".split()
)
