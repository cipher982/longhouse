"""The universal harness's default input, and the reply it asks for.

A module of its own so that a caller which only has to send the input, such
as Cursor Gate 0, reads it here instead of importing the harness.
"""

from __future__ import annotations

# The default input is an instruction with one right answer, so a provider
# that receives it has a turn it can finish. The phrase is the reply it asks
# for; the prompt is built from it so the two cannot drift.
HARNESS_REPLY_PHRASE = "LONGHOUSE UNIVERSAL HARNESS"
DEFAULT_HARNESS_PROMPT = f"Reply with exactly {HARNESS_REPLY_PHRASE} and nothing else."
