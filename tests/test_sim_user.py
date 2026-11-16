"""The user simulator, against transcripts that caught it out.

`looks_like_question` used to be `"?" in text[-400:]`, and `pick_answer` was
stateless. Between them they scored gpt-oss at 0.100 on three tasks for asking
clarifying questions — which `pegasus-scaffold` instructs models to do.

    python3 -m tests.test_sim_user
"""

from __future__ import annotations

import json
import os

from harness.trial import EXHAUSTED, looks_like_question, pick_answer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def first_reply(trial: str) -> str:
    path = os.path.join(ROOT, "runs", trial, "transcript.jsonl")
    for line in open(path):
        entry = json.loads(line)
        if entry["kind"] == "assistant":
            return entry["payload"].get("text") or ""
    return ""


ASKING = [
    # (trial, is the model waiting on the user?)
    ("t01-nrp/t01-sensor-summary--gpt-oss--r0", True),        # list, then "Let me know"
    ("t02-fabric/t02-tile-reduce--gpt-oss-20b--r0", True),    # "Let's start by filling in those details."
    ("t02-nrp/t02-tile-reduce--gpt-oss--r0", True),           # ends with a question
    # Both of these did substantial work *and* closed with a question. Waiting on
    # the user is not the opposite of having worked, so True is right for them.
    ("t01b-nrp/t01-sensor-summary--kimi--r0", True),          # "Is that okay, or ...?"
    ("t03-nrp/t03-legacy-tool-container--kimi--r0", True),    # "do you have a preference ...?"
    # A genuine negative: 64 tool calls, finished, hands nothing back.
    ("t01-azure/t01-sensor-summary--Kimi-K2.6--r0", False),
]

BANK = {
    "tile_count": "Between 500 and 2,000 depending on the survey.",
    "merge_shape": "Whatever avoids one giant final step — you decide.",
    "failures": "A few bad tiles are normal.",
    "default": "Your call.",
}


def main() -> None:
    fails = 0
    for trial, want in ASKING:
        got = looks_like_question(first_reply(trial))
        ok = got == want
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  asking={str(got):5s} want={str(want):5s}  {trial}")

    # A model that asks about several things in turn must not be handed the same
    # answer twice — that is what stalled gpt-oss on t02 for three turns.
    print()
    asked = ["How many tiles?", "What merge shape?", "What about failures?", "Anything else?"]
    used: set[str] = set()
    keys = []
    for q in asked:
        key, _ = pick_answer(q, BANK, used)
        keys.append(key)
        used.add(key)
    # No key may be handed over twice, `default` included.
    repeated = len(keys) != len(set(keys))
    fails += repeated
    print(f"  {'FAIL' if repeated else 'PASS'}  keys across four questions: {keys}")

    # Once the bank is spent, the simulator must stop rather than repeat itself.
    # On t13, `kimi` was told "Tell me what you'd recommend and why." twice in a
    # row, spent a round restating its previous answer, and tripped
    # `rounds_exhausted` — a harness artefact scored as model behaviour.
    used = set(BANK)
    key, text = pick_answer("And one more thing?", BANK, used)
    ok = key == EXHAUSTED and text == ""
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  spent bank yields {key!r}, not a repeat")

    # A bank with unused entries must never short-circuit to exhaustion.
    key, _ = pick_answer("Anything?", BANK, {"default"})
    ok = key != EXHAUSTED
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  unused answers still offered: {key!r}")

    total = len(ASKING) + 3
    print(f"\n{total - fails}/{total} passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
