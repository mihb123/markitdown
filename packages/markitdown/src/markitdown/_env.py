"""
Configuration lookup shared by the converters and the HTTP API.

A value is read from the real environment first, then from the `.env` files
looked up via $MARKITDOWN_ENV_FILE, a `.env` in the current directory or any
parent directory, and finally ~/.config/markitdown/.env. The earliest file that
defines a key wins.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional


# Parse a simple KEY=VALUE `.env` file, ignoring blanks and `#` comments.
def load_env_file(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


# Build the ordered list of candidate `.env` paths (see module docstring).
def candidate_env_files() -> List[str]:
    candidates = []
    explicit = os.getenv("MARKITDOWN_ENV_FILE")
    if explicit:
        candidates.append(explicit)

    directory = os.getcwd()
    while True:
        candidates.append(os.path.join(directory, ".env"))
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent

    candidates.append(os.path.expanduser("~/.config/markitdown/.env"))
    return candidates


# Merge every readable candidate file into one mapping.
def env_file_values() -> Dict[str, str]:
    values: Dict[str, str] = {}
    for path in candidate_env_files():
        if path and os.path.isfile(path):
            for key, value in load_env_file(path).items():
                values.setdefault(key, value)
    return values


# Resolve a single key against the environment and the `.env` files.
def resolve(key: str, default: Optional[str] = None) -> Optional[str]:
    from_env = os.environ.get(key)
    if from_env:
        return from_env
    return env_file_values().get(key) or default
