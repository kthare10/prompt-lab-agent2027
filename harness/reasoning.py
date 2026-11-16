"""Reasoning-delimiter hygiene for captured assistant text.

Some models emit chain-of-thought delimiters into the visible content stream.
In this corpus the leak is one-sided: 216 `</think>` closers across 20 of 359
trials and *zero* `<think>` openers anywhere, because the upstream server
consumes the opener and passes the closer through.

That asymmetry decides the repair. The prose on *both* sides of a stray closer
is real model output — read one and it is answer text addressed to the user,
not scratchpad — so we remove the marker and keep the words. The tempting rule,
"everything before an unmatched closer was thinking", would silently delete
answers, which is a worse corruption than the one being fixed.

Harmony channel markers (`<|channel|>`, `<|message|>`, ...) are deliberately
NOT touched. In the three trials that carry them, gpt-oss-20b is *discussing*
the harmony format in prose while working out why its tool calls stopped being
recognised. There they are subject matter, not structure, and stripping them
would mangle the sentences that explain a real failure.

`strip_reasoning` is idempotent, so it is safe to apply both at capture (so new
trials are clean) and at read (so the trials already on disk are repaired
without re-running them).
"""

from __future__ import annotations

import re

# Delimiter names seen in the wild for chain-of-thought blocks. Matched with a
# word boundary so `<thinking-cap>` in prose is not mistaken for a delimiter.
_NAMES = "think|thinking|reasoning"

# A well-formed block: opener through closer, non-greedy, newlines included.
# None appear in the corpus today (every opener is consumed upstream), but a
# server-side change would start delivering them and the block is genuinely
# reasoning, so it goes.
# The name must be followed by the end of the tag or an attribute, never by
# more name characters: `<thinking-cap>` in prose is not a delimiter, and `\b`
# alone happily matches the boundary before the hyphen.
_TAG = rf"(?:{_NAMES})(?:\s[^>]*)?"

_PAIRED = re.compile(rf"[ \t]*<({_NAMES})(?:\s[^>]*)?>.*?</\1\s*>[ \t]*",
                     re.DOTALL | re.IGNORECASE)

# Whatever delimiters survive are orphans. Take the horizontal whitespace on
# either side with them and leave a single space: newlines are excluded from
# the character class on purpose, so paragraph breaks and code-block indentation
# survive the edit.
_ORPHAN = re.compile(rf"[ \t]*</?{_TAG}>[ \t]*", re.IGNORECASE)

_ANY = re.compile(rf"</?{_TAG}>", re.IGNORECASE)


def has_reasoning_leak(text: str | None) -> bool:
    """True if `text` carries chain-of-thought delimiters."""
    return bool(text) and bool(_ANY.search(text))


def strip_reasoning(text: str | None) -> str:
    """Remove reasoning delimiters, preserving the surrounding prose.

    Idempotent: the output contains no delimiters, so a second application is
    a no-op.
    """
    if not text:
        return text or ""
    cleaned = _PAIRED.sub(" ", text)
    cleaned = _ORPHAN.sub(" ", cleaned)
    return cleaned.strip()
