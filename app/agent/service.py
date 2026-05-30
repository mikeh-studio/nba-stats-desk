from __future__ import annotations

import json
from typing import Any

from app.agent.tools import StatsToolRunner, get_tool_schemas
from app.config import Settings
from app.repository import WarehouseRepository

SYSTEM_PROMPT = """
You are an NBA stats analyst for the 2025-26 NBA Stats Desk site.
Answer only from the provided tool results and curated gold-model semantics.
Use tools for player identity, game logs, percentiles, trends, rankings, and similarity.
Use calculate_player_percentile for questions asking where one player ranks in a metric cohort.
For "points attributed", "points created", or "points + assists * 2", use metric points_created.
For game-by-game questions, call get_player_game_log so the response can include each game's values.
For date-range questions, pass start_date and end_date as YYYY-MM-DD tool arguments; use null for an open side of the range.
Respect explicit minimum-games filters; if the cohort is empty or the player is outside it, say that directly.
Do not invent SQL, raw table names, injuries, transactions, or facts not present in tool data.
If a player name is ambiguous, ask the user to choose from the matches.
Copy relevant tool chart/table payloads into the final structured response.
Keep answers direct and useful for NBA fans comparing player form against the league.
""".strip()


AGENT_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "columns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "label": {"type": "string"},
                            },
                            "required": ["key", "label"],
                            "additionalProperties": False,
                        },
                    },
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "required": ["title", "columns", "rows"],
                "additionalProperties": False,
            },
        },
        "charts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["line", "bar"]},
                    "title": {"type": "string"},
                    "x_label": {"type": "string"},
                    "y_label": {"type": "string"},
                    "series": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "label": {"type": "string"},
                                "points": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "x": {"type": "string"},
                                            "y": {"type": "number"},
                                            "meta": {"type": "string"},
                                        },
                                        "required": ["x", "y", "meta"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": ["key", "label", "points"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["type", "title", "x_label", "y_label", "series"],
                "additionalProperties": False,
            },
        },
        "metric_definitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "label": {"type": "string"},
                    "definition": {"type": "string"},
                },
                "required": ["key", "label", "definition"],
                "additionalProperties": False,
            },
        },
        "followups": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "answer",
        "assumptions",
        "tables",
        "charts",
        "metric_definitions",
        "followups",
    ],
    "additionalProperties": False,
}

TEXT_FORMAT = {
    "format": {
        "type": "json_schema",
        "name": "nba_stats_agent_answer",
        "schema": AGENT_ANSWER_SCHEMA,
        "strict": True,
    }
}


class AgentDisabledError(RuntimeError):
    """Raised when the stats agent is not configured to call OpenAI."""


class AgentExecutionError(RuntimeError):
    """Raised when an OpenAI agent run fails."""


def _default_agent_answer(answer: str) -> dict[str, Any]:
    return {
        "answer": answer,
        "assumptions": [],
        "tables": [],
        "charts": [],
        "metric_definitions": [],
        "followups": [],
    }


def normalize_agent_answer(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _default_agent_answer(str(payload or "No answer returned."))
    answer = _default_agent_answer(str(payload.get("answer") or "No answer returned."))
    for key in ("assumptions", "tables", "charts", "metric_definitions", "followups"):
        value = payload.get(key)
        answer[key] = value if isinstance(value, list) else []
    return answer


class StatsAgent:
    def __init__(
        self,
        settings: Settings,
        repo: WarehouseRepository,
        *,
        client: Any | None = None,
        tool_runner: StatsToolRunner | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.client = client
        self.tool_runner = tool_runner or StatsToolRunner(repo)

    def _get_client(self) -> Any:
        if self.client is not None:
            return self.client
        if not self.settings.openai_agent_enabled:
            raise AgentDisabledError("The OpenAI stats agent is disabled.")
        if not self.settings.openai_api_key:
            raise AgentDisabledError("OPENAI_API_KEY is required to use Ask NBA Stats.")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise AgentDisabledError(
                "The openai package is not installed. Run pip install -r requirements.txt."
            ) from exc
        self.client = OpenAI(api_key=self.settings.openai_api_key)
        return self.client

    def answer(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        cleaned_question = question.strip()
        if not cleaned_question:
            raise ValueError("Question must not be blank.")

        client = self._get_client()
        input_messages: list[Any] = [{"role": "user", "content": cleaned_question}]
        tool_calls: list[dict[str, str]] = []
        max_tool_calls = max(1, self.settings.agent_max_tool_calls)

        while len(tool_calls) <= max_tool_calls:
            try:
                response = client.responses.create(
                    model=self.settings.openai_agent_model,
                    instructions=SYSTEM_PROMPT,
                    input=input_messages,
                    tools=get_tool_schemas(),
                    text=TEXT_FORMAT,
                )
            except Exception as exc:
                raise AgentExecutionError(
                    f"OpenAI agent request failed: {exc}"
                ) from exc

            output_items = list(getattr(response, "output", []) or [])
            function_calls = [
                item
                for item in output_items
                if getattr(item, "type", None) == "function_call"
            ]
            if not function_calls:
                output_text = str(getattr(response, "output_text", "") or "")
                try:
                    parsed = json.loads(output_text)
                except json.JSONDecodeError:
                    parsed = _default_agent_answer(output_text or "No answer returned.")
                payload = normalize_agent_answer(parsed)
                payload["tool_calls"] = tool_calls
                payload["conversation_id"] = conversation_id
                return payload

            input_messages.extend(output_items)
            for call in function_calls:
                if len(tool_calls) >= max_tool_calls:
                    break
                name = str(getattr(call, "name", ""))
                try:
                    args = json.loads(getattr(call, "arguments", "{}") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self.tool_runner.call_tool(name, args)
                tool_calls.append(
                    {"name": name, "status": str(result.get("status", "ok"))}
                )
                input_messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": getattr(call, "call_id", ""),
                        "output": json.dumps(result, default=str),
                    }
                )

        payload = _default_agent_answer(
            "I hit the tool-call limit before finishing. Try a narrower question."
        )
        payload["tool_calls"] = tool_calls
        payload["conversation_id"] = conversation_id
        return payload
