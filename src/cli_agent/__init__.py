"""Natural-language -> CLI command agent.

The interesting part of this package is not the code, it is prompts/. The code
exists to (a) call the model, (b) measure what came back, and (c) refuse to run
anything dangerous no matter what the model said.
"""

from .agent import AgentResult, Decision, generate
from .safety import SafetyLevel, SafetyVerdict, classify

__all__ = [
    "AgentResult",
    "Decision",
    "SafetyLevel",
    "SafetyVerdict",
    "classify",
    "generate",
]
