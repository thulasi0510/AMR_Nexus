"""Retrieval router.

Each source gets a small adapter. The LangGraph orchestrator decides which
adapter to call; this module only translates the plan into source calls.
"""
from agents.literature import search_literature


def run_literature(plan):
    return search_literature(plan)
