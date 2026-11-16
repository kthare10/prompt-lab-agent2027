# Task suite

One YAML per task. The runner reads `prompt` verbatim as the first user message,
answers follow-up questions from `sim_answers`, then grades the produced project
against `expect` (mechanically, L0) and `rubric` (by judge, L1).

## Why the prompts are written in a scientist's voice

Studio's users are domain scientists, not workflow engineers — the environment
overlay is explicit about that, down to a say/rather-than vocabulary table. A
prompt written in Pegasus jargon ("build a DAG with a fan-in merge over the
per-sample BAMs") measures something Studio's actual users never do. Every
prompt here says what the science needs and leaves the Pegasus mapping to the
model, which is the thing under test.

## Exposure — read this before interpreting any score

The first draft of this suite split tasks into "in corpus" and "held out", on the
theory that only the 13 shipped generators leak answers. That was too coarse.
**The skills and the always-on guide were themselves written from those
workflows**, so a pattern can reach the model without the example file being
readable at all.

There are four distinct exposure levels, and every task declares which it is
along with the `exposure_evidence` that establishes it:

| Level | The model can get the answer from | Measures |
|---|---|---|
| `exemplified` | A shipped generator implements this exact pipeline. | Recall of a file. |
| `documented` | The pattern is generalised in always-on context — `PEGASUS.md` or the Studio overlay — with no matching example. | Applying a documented rule to a new domain. |
| `pointed` | A skill names the exemplar that solves it, **by filename only**, and the file is unreachable from chat (C9). | Whether a pointer alone is worth anything. |
| `undocumented` | Nothing. Absent from the guide, the overlay, every SKILL.md and all 13 examples. | Generalisation past the knowledge base. |

Current balance: 10 `documented`, 2 `pointed`, 2 `undocumented`, 1 `exemplified`.

That weighting is deliberate. Studio's actual claim is *"the knowledge base makes
these models capable"* — so applying a documented pattern to an unseen domain is
the product-relevant measurement, and it should dominate. But it cannot tell you
whether a model can reason past the corpus, which is why t13 and t14 exist.

**t05a/t05b are the same task at two levels** — the tnseq conversion that
`pegasus-convert` names explicitly, and a structurally identical photometry
pipeline that is merely `documented`. The gap between a model's two scores is the
memorisation metric. Report the pair; never average it.

**The `pointed` pair is the most diagnostic.** For t02 and t12 the skill names
exactly the right exemplar — `workflow_generator_sra_search.py` for a
hierarchical merge tree, `workflow_generator_medical_imaging_fl.py` for
SubWorkflows — and then `read_file` refuses to open it. If scores on these two
jump once the examples are unlocked, C9 is costing real quality; if they don't,
the filename pointer was never doing any work and the fix is elsewhere.

## Field reference

| Field | Meaning |
|---|---|
| `id`, `title` | Stable identifiers; `id` names the trial directory. |
| `tier` | `smoke` (Stage 0), `core` (Stage 1), `stretch` (Stage 1, long horizon). |
| `exposure` | `exemplified`, `documented`, `pointed` or `undocumented` — see above. |
| `exposure_evidence` | The grep-able fact establishing that level. Re-check it when the knowledge base changes. |
| `skill` | Slash command prefixed to the prompt, or `null` for none. |
| `agent` | Chat persona id; `general` unless the task is about a persona. |
| `project` | The directory name the prompt supplies. Trial id is appended at run time (C7). |
| `prompt` | Verbatim first user message. |
| `sim_answers` | Intent → fixed reply, for the user simulator. `default` catches anything unmatched. |
| `expect` | Mechanical checks: `files`, `jobs`, `dag`, `must`, `must_not`. |
| `rubric` | What the L1 judge weighs for this task. |
| `inspired_by` | The example that shaped it, for provenance. |
| `depth` | How far the run-flow goes: `generate`, `plan`, or `run`. |
