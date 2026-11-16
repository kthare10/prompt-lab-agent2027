"""The chat engine decides what "the model wrote a file" means.

`studio-webapp` main moved the chat panel onto `opencode serve`. OpenCode brings
its own tools — `write`, `edit`, `bash`, `read`, `glob` — where the builtin loop
had `write_file`, `run_command` and three others. Graded against the builtin
vocabulary an OpenCode transcript reports that the model wrote nothing and
called five tools that do not exist: a total protocol failure that belongs
entirely to the grader.

    python3 -m tests.test_engine_vocabulary
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.graders.mechanical import (engine_tools, protocol_checks,  # noqa: E402
                                        write_paths)

# One turn as OpenCode reports it, after `harness/opencode.py` has split each
# mutating tool part into a call. `filePath` is what its tools name the
# destination; the extractor accepts the other spellings too rather than betting
# the whole `wrote_files` check on one string.
OPENCODE = [
    {"id": "call_1", "name": "read", "arguments": {"filePath": "PEGASUS.md"}},
    {"id": "call_2", "name": "write",
     "arguments": {"filePath": "workflows/sensor-summary/workflow_generator.py",
                   "content": "from Pegasus.api import *"}},
    {"id": "call_3", "name": "bash", "arguments": {"command": "ls workflows"}},
    {"id": "call_4", "name": "edit",
     "arguments": {"filePath": "workflows/sensor-summary/README.md"}},
]
BUILTIN = [
    {"id": "c1", "name": "read_file", "arguments": {"path": "PEGASUS.md"}},
    {"id": "c2", "name": "write_file",
     "arguments": {"path": "workflows/sensor-summary/workflow_generator.py",
                   "content": "x"}},
    {"id": "c3", "name": "run_command", "arguments": {"command": "ls"}},
]


def main() -> None:
    fails = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal fails
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))

    got = write_paths(OPENCODE, "opencode")
    check("an OpenCode write is a write", len(got) == 2, str(got))
    check("and its destination survives",
          got[0].endswith("workflow_generator.py"), str(got))
    check("a shell call is not a write", "ls workflows" not in "".join(got))

    clean = protocol_checks(OPENCODE, True, "opencode")
    check("OpenCode's own tools are known to it",
          clean["protocol_clean"] and clean["unknown_tool_calls"] == 0,
          str(clean["unknown_tools"]))

    # The failure this guards against, stated as a test: same transcript, wrong
    # vocabulary.
    wrong = protocol_checks(OPENCODE, True, "builtin")
    check("graded as builtin it would be four unknown calls",
          wrong["unknown_tool_calls"] == 4 and not wrong["protocol_clean"])
    check("and would report no writes at all", write_paths(OPENCODE, "builtin") == [])

    # And the builtin vocabulary still works, unchanged.
    check("builtin writes still count", write_paths(BUILTIN, "builtin") ==
          ["workflows/sensor-summary/workflow_generator.py"])
    check("builtin protocol still clean",
          protocol_checks(BUILTIN, True, "builtin")["protocol_clean"])
    check("an unknown engine falls back to builtin",
          engine_tools("nonsense") is engine_tools("builtin"))

    print(f"\n{9 - fails}/9 passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
