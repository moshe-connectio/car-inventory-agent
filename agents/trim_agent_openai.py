"""
agents/trim_agent_openai.py
Thin entry point — delegates to trim_openai package.
"""
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TrimAgent] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.trim_openai.runner import run  # noqa: F401 — re-exported for tasks.py

__all__ = ["run"]
