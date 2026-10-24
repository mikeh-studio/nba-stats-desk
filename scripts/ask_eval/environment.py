"""Load only provider credentials from a local file, not application settings."""

import os
from pathlib import Path

from dotenv import dotenv_values


def select_code_root(module_file, environ=os.environ):
    """Return (runner_root, code_root) for the evaluation server.

    The runner root is the harness's own checkout. A consumer that reaches the
    harness through a symlink must name ASK_EVAL_CODE_ROOT; otherwise the
    harness's checkout would be evaluated silently instead of the consumer's.
    """
    runner = Path(module_file).resolve().parents[2]
    explicit = environ.get("ASK_EVAL_CODE_ROOT")
    if explicit:
        return runner, Path(explicit)
    invoked = Path(os.path.abspath(module_file)).parents[2]
    if invoked.resolve() != runner:
        raise RuntimeError(
            f"The evaluation harness at {runner} was reached from {invoked}. "
            "Set ASK_EVAL_CODE_ROOT to the checkout whose app code to evaluate."
        )
    return runner, runner


def load_provider_credentials(path):
    values = dotenv_values(path, interpolate=False)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        if values.get(key):
            os.environ.setdefault(key, values[key])
