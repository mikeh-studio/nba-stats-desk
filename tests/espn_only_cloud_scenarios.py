"""Compatibility entry point; manual runner lives under tests/manual/."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).parent / "manual" / Path(__file__).name), run_name="__main__"
    )
