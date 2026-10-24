"""Compatibility names for public scope/conversation policy.

Production classification moved to the optional provider. New public consumers
import scope_policy directly; this module can be retired with legacy imports.
"""

from app.agent.scope_policy import (
    explicit_aggregation as explicit_aggregation,
)
from app.agent.scope_policy import (
    has_specific_metric as has_specific_metric,
)
from app.agent.scope_policy import (
    metric_terms as metric_terms,
)
from app.agent.scope_policy import (
    name_reply as name_reply,
)
from app.agent.scope_policy import (
    name_tokens as name_tokens,
)
from app.agent.scope_policy import (
    names_other_player as names_other_player,
)
from app.agent.scope_policy import (
    round_scope_message as round_scope_message,
)
from app.agent.scope_policy import (
    split_kind as split_kind,
)
from app.agent.scope_policy import (
    starts_new_question as starts_new_question,
)
from app.agent.scope_policy import (
    task_terms as task_terms,
)
from app.agent.scope_policy import (
    venue_mentions as venue_mentions,
)
