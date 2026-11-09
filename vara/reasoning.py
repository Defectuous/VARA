"""Decide, per message, whether the model should reason before answering.

Reasoning makes answers to hard questions better but costs time: on qwen3:8b a short reply takes about
0.1 s without it and several seconds with it. Greetings, small talk, and quick facts don't need it;
questions that ask why, compare options, involve code or numbers, or are long do. Spoken replies
favour speed, so only clearly hard questions reason there.
"""

import re
from typing import Literal

ReasoningMode = Literal["auto", "on", "off"]

# Questions that need working through, even when spoken.
_DEEP = re.compile(
    r"\b(calculate|compute|solve|prove|derive|debug|troubleshoot|diagnose|plan|design|architect"
    r"|compare|analy[sz]e|optimi[sz]e|refactor|evaluate|pros and cons|trade-?offs?|step[- ]by[- ]step"
    r"|think (?:it )?(?:through|carefully|hard)|reason (?:through|about)|work (?:it )?out)\b",
    re.IGNORECASE,
)
# Questions that benefit from reasoning when there's time (typed, not spoken).
_LIGHT = re.compile(
    r"\b(why|explain|difference between|should i|which (?:is|one|would)|best way|recommend|review"
    r"|how (?:do|does|did|would|could|can|should|to)\b.{0,60}\b(?:work|fix|build|make|set ?up"
    r"|implement|configure|install|choose|improve))\b",
    re.IGNORECASE,
)
_CODE = re.compile(
    r"```|\btraceback\b|\b(?:def|class|function|select|import)\s+\w|[{};]\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_MATH = re.compile(
    r"\d\s*[-+*/^%=×÷]\s*\d|\b(?:equation|integral|derivative|probability|percentage|average of)\b",
    re.IGNORECASE,
)
_QUICK = re.compile(
    r"^\W*(?:hi|hello|hey|yo|thanks|thank you|cheers|ok(?:ay)?|yes|yeah|no|nope|sure|cool|nice|great"
    r"|good (?:morning|afternoon|evening|night)|what(?:'s| is) the (?:time|date|day)|what time is it)\b",
    re.IGNORECASE,
)
_LONG_WORDS = 40  # a typed message this long is usually a detailed request


def should_reason(text: str, *, spoken: bool = False, mode: ReasoningMode = "auto") -> bool:
    """True if the model should reason before answering `text`."""
    if mode != "auto":
        return mode == "on"
    words = len(text.split())
    if _QUICK.search(text) and words <= 8:
        return False
    if _DEEP.search(text) or _CODE.search(text) or _MATH.search(text):
        return True
    if spoken:
        return False
    return bool(_LIGHT.search(text)) or words >= _LONG_WORDS
