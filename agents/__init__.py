"""Person B: AI agents. Pure functions, no messaging code."""
from agents.intake import intake
from agents.parts import find_parts
from agents.quote import quote

__all__ = ["intake", "find_parts", "quote"]
