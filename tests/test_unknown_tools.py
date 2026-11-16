"""A call naming a tool Studio does not have is a protocol failure, not a no-op.

`gpt-oss-20b` emitted `write_file<|channel|>commentary` — a Harmony channel
marker leaking into the tool name — for 2 of its 6 calls on one t01 trial.
Studio cannot dispatch that, so those two writes never happened. L0 recorded
`malformed_tool_calls: 0`, `tool_errors: 0`, `protocol_clean: True`: the name
was in neither REQUIRED_ARGS (so arguments were never checked) nor WRITE_TOOLS
(so it was never counted as a write). A third of the model's calls vanished and
the protocol scored perfect.

    python3 -m tests.test_unknown_tools
"""

from __future__ import annotations

import json
import os

import yaml

from harness.graders.mechanical import KNOWN_TOOLS, grade

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def graded(trial: str, task_id: str) -> dict:
    task = yaml.safe_load(open(os.path.join(ROOT, "tasks", f"{task_id}.yaml")))
    path = os.path.join(ROOT, "runs", trial, "transcript.jsonl")
    turns = [json.loads(l)["payload"] for l in open(path)
             if json.loads(l)["kind"] == "assistant"]
    # A directory that cannot exist: this asserts on the transcript-derived
    # checks only, so it must not accidentally read a real workspace.
    return grade(task, os.path.join(ROOT, "runs", "__no_such_project__"), turns)


def main() -> None:
    fails = 0

    # Studio's TOOL_DEFINITIONS, mirrored. If Studio grows a tool and this set
    # does not, every call to it is wrongly flagged — so the mirror is the risk
    # this check carries, and it is worth stating.
    expected = {"write_file", "read_file", "list_directory", "run_command", "create_directory"}
    ok = KNOWN_TOOLS == expected
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  KNOWN_TOOLS mirrors Studio: {sorted(KNOWN_TOOLS)}")

    cases = [
        # (trial, task, unknown calls expected, protocol_clean expected)
        ("t01-fabric/t01-sensor-summary--gpt-oss-20b--r0", "t01-sensor-summary", 2, False),
        ("t01b-nrp/t01-sensor-summary--kimi--r0", "t01-sensor-summary", 0, True),
        ("t02-nrp/t02-tile-reduce--kimi--r0", "t02-tile-reduce", 0, True),
    ]
    for trial, task_id, want_n, want_clean in cases:
        c = graded(trial, task_id)
        got_n, got_clean = c["unknown_tool_calls"], c["protocol_clean"]
        good = got_n == want_n and got_clean == want_clean
        fails += not good
        print(f"  {'PASS' if good else 'FAIL'}  unknown={got_n} (want {want_n}) "
              f"clean={got_clean} (want {want_clean})  {c.get('unknown_tools') or ''}  {trial}")

    print(f"\n{len(cases) + 1 - fails}/{len(cases) + 1} passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
