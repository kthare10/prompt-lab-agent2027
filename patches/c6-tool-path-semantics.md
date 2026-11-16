### The path you pass to a tool is not the path on disk

Your file tools take a path **relative to `$HOME/work`**, and they do **not** expand
`~` or `$HOME`. The location above is where files end up; it is not what you type.

| Pass this | Not this |
|---|---|
| `workflows/<name>/workflow_generator.py` | `~/work/workflows/<name>/workflow_generator.py` |
| `workflows/<name>/bin/run.sh` | `$HOME/work/workflows/<name>/bin/run.sh` |
| `workflows/<name>/README.md` | `work/workflows/<name>/README.md` |
| | `<name>/workflow_generator.py` |

A real absolute path also works: `/home/ubuntu/work/workflows/<name>/...`.

Every wrong form fails **silently** — the tool reports success and the transcript
looks correct:

- `~/work/…` creates a directory **literally named `~`**, at
  `$HOME/work/~/work/workflows/<name>/`
- `$HOME/work/…` does the same with a literal `$HOME`
- `work/…` doubles the prefix: `$HOME/work/work/workflows/<name>/`
- `<name>/…` drops the `workflows/` level: `$HOME/work/<name>/`

All of them land outside the one directory the Workflows tab scans, so the project
gets no Generate, Plan or Run button. Nothing errors. The files are simply
somewhere the interface never looks.

**Check yourself before reporting done**: `ls workflows/<name>` must list your
files. If it does not, you wrote them somewhere else.
