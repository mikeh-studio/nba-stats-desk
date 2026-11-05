#!/usr/bin/env python3
"""Compatibility entry point for the explicit local server command."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.local_server import main as serve  # noqa: E402


def main():
    serve(["weekly", *sys.argv[1:]])


if __name__ == "__main__":
    main()
