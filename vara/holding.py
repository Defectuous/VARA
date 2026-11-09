"""Holding lines: what V.A.R.A. says while a slow reply is on its way.

Most replies start in well under a second, but some don't: a model loading into GPU memory after a switch
(about a minute), long reasoning, or a web search first. When nothing has arrived after a few seconds the
chat stream sends a `notice` with one of these lines, then another every couple of minutes while it's still
quiet, and then the reply itself. The lines are fixed text, not model output: the model is the thing
that's busy.
"""

import random
from collections.abc import Sequence

# With a personality (VARA_PERSONALITY set), she gets to be a little snarky about the wait.
FIRST_SNARKY = (
    "Hang on, this one's going to take a minute. Good things, patience, all that.",
    "Give me a minute on this one. I'm thinking, which is more than I can say for some people.",
    "This is going to take a minute. Feel free to stare at the screen meaningfully.",
    "Ooh, a hard one. Give me a minute.",
    "Bear with me, this one needs a minute. Go stretch or something.",
)
STILL_SNARKY = (
    "Still working on it. I haven't forgotten you, I promise.",
    "Still on it. Genius takes time, apparently.",
    "Still working on it. Not ignoring you, just thinking very hard.",
    "Still going. This one's putting up a fight.",
)
FIRST_PLAIN = (
    "This is going to take a minute.",
    "Give me a minute on this one.",
    "One moment, this one will take a little while.",
)
STILL_PLAIN = (
    "Still working on it.",
    "Still on it, almost there I hope.",
    "Still working on your answer.",
)


def holding_line(round_: int, *, personality: bool, rng: random.Random | None = None) -> str:
    """The line for the `round_`-th notice of one reply: 0 is the first heads-up, later ones are
    "still working" updates."""
    pick = (rng or random).choice
    if round_ == 0:
        lines: Sequence[str] = FIRST_SNARKY if personality else FIRST_PLAIN
    else:
        lines = STILL_SNARKY if personality else STILL_PLAIN
    return pick(lines)
