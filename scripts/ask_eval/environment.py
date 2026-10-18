"""Load only provider credentials from a local file, not application settings."""

import os

from dotenv import dotenv_values


def load_provider_credentials(path):
    values = dotenv_values(path, interpolate=False)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        if values.get(key):
            os.environ.setdefault(key, values[key])
