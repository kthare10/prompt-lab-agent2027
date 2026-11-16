"""Does the reasoning-delimiter stripper keep the prose and drop the markers?

The corpus case that motivated it: 216 `</think>` closers across 20 of 359
trials, with *zero* openers anywhere, because the opener is consumed upstream.
That asymmetry is why the stripper cannot treat "text before an unmatched
closer" as scratchpad — on inspection that text is answer prose addressed to
the user, and dropping it would corrupt the record worse than the leak does.

    python3 -m tests.test_reasoning
"""

from __future__ import annotations

import glob
import json
import os

from harness.reasoning import has_reasoning_leak, strip_reasoning
from harness.store import RUNS_DIR

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def corpus() -> list[str]:
    """Every transcript reachable from here, live results and committed alike.

    The two live at different paths depending on where this runs — the results
    directory is `/home/ubuntu/prompt-lab-runs` on the host and
    `/opt/prompt-lab-runs` inside the container — and `runs/` in the repo holds
    the records `sync_runs.sh` last backed up. Hardcoding either one made this
    test find nothing in the other environment and fail for the wrong reason.
    """
    seen = dict.fromkeys(
        glob.glob(os.path.join(RUNS_DIR, "*", "*", "transcript.jsonl"))
        + glob.glob(os.path.join(ROOT, "runs", "*", "*", "transcript.jsonl")))
    return list(seen)

CASES: list[tuple[str, str, str]] = [
    # (label, input, expected)
    ("orphan closer keeps both sides",
     "No failure, no empty files. </think>   The test data generator wrote",
     "No failure, no empty files. The test data generator wrote"),
    ("paired block removed whole",
     "before <think>scratchpad musing</think> after",
     "before after"),
    # gpt-oss-20b discusses the harmony format in prose while working out why
    # its tool calls stopped being recognised: subject matter, not structure.
    ("harmony markers untouched",
     "earlier calls were `assistant<|channel|>commentary to=functions.write_file`",
     "earlier calls were `assistant<|channel|>commentary to=functions.write_file`"),
    ("lookalike tag spared",
     "wear a <thinking-cap> today",
     "wear a <thinking-cap> today"),
    ("empty is empty", "", ""),
]


def main() -> None:
    fails = 0

    for label, src, want in CASES:
        got = strip_reasoning(src)
        ok = got == want
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            print(f"        got  {got!r}\n        want {want!r}")

    # Newlines are outside the whitespace class on purpose: paragraph breaks and
    # code indentation have to survive, because the grader reads code blocks.
    out = strip_reasoning("para one.\n\n</think>\n\n```python\n    indented = 1\n```")
    ok = "\n\n" in out and "    indented = 1" in out and "</think>" not in out
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  newlines and indentation survive")

    once = strip_reasoning("a </think> b <think>c</think> d")
    ok = strip_reasoning(once) == once
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  idempotent")

    ok = (has_reasoning_leak("x </think> y") and not has_reasoning_leak("plain")
          and not has_reasoning_leak("") and not has_reasoning_leak(None)
          and strip_reasoning(None) == "")
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  detection and empty inputs")

    # Against the real corpus, not a fixture. The guarantee is stated on
    # non-whitespace characters: these turns carry huge runs of padding spaces
    # around the delimiters, so a raw length ratio measures the padding rather
    # than the prose. Every non-space character removed must be part of a
    # delimiter — 8 per `</think>` — and nothing else.
    hits, damaged = 0, []
    for tj in corpus():
        for line in open(tj):
            entry = json.loads(line)
            if entry.get("kind") != "assistant":
                continue
            text = entry["payload"].get("text") or ""
            if not has_reasoning_leak(text):
                continue
            hits += 1
            cleaned = strip_reasoning(text)
            if has_reasoning_leak(cleaned):
                damaged.append(f"residual delimiter in {tj}")
                continue
            dense = lambda t: len("".join(t.split()))
            lost = dense(text) - dense(cleaned)
            expected = 8 * text.count("</think>") + 7 * text.count("<think>")
            if lost != expected:
                damaged.append(f"{tj}: lost {lost} non-space chars, expected {expected}")
    ok = hits > 0 and not damaged
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  corpus: {hits} leaking turns, "
          f"{len(damaged)} damaged")
    for d in damaged[:5]:
        print(f"        {d}")

    total = len(CASES) + 4
    print(f"\n{total - fails}/{total} passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
