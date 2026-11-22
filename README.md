# ReleaseScribe

An OpenMinion agent that drafts structured release notes from git history —
and writes `CHANGELOG.md` **only after a human approves the exact draft**.

Built for **Cortensor Hackathon #5** with the OpenMinion `0.1.22` Python SDK.
Free models only; no paid inference, no chain, no deployment.

## What it does

```
$ python app.py ./fixtures/sample-repo/repo v0.1.0..HEAD
```

1. **Read** — the agent calls the `read_commits` tool (`git log`, read-only)
   for the repo + range you give it.
2. **Draft** — it returns typed `ReleaseNotes` (Pydantic): `summary`,
   `features[]`, `fixes[]`, `breaking[]`, `notes[]`. Every bullet must trace
   to a commit; the agent is instructed never to invent items.
3. **Review** — a reviewer subagent re-reads the raw commits and flags any
   draft item with no backing commit (hallucination check).
4. **Refine** — in the same session, tell it `refine <text>` (e.g.
   `refine perpendek summary`) and it updates the draft with full context.
5. **Approve** — you see the exact rendered Markdown and answer `approve`.
6. **Write** — only then does the agent call `write_changelog`, gated twice:
   OpenMinion's `approval_callback` **and** an in-process content gate that
   refuses any content the operator did not approve verbatim.

One `session_id` per repo+range, so refinements share history and two
different repos never share a session.

## Workflow (OpenMinion SDK)

- `Agent(instructions=..., tools=[...], forced_tools=[...], output_type=ReleaseNotes, config_path=..., session_id=...)`
- Tools are plain Python functions decorated with `@tool`:
  `read_commits` is `min_scope="READ_ONLY", idempotent=True`;
  `write_changelog` is `min_scope="WRITE_SAFE", idempotent=False`.
- Provider fallback chain (see `releasescribe/providers.py`):
  OpenRouter (`baidu/ernie-5.5-flash-preview:free`, primary) →
  100milionseek (`deepseek-v4.1-flash-free`) → Coarena
  (`deepseek-v4-1-flash-1017`). A `ProviderError`/`TurnTimeoutError` on one
  rung moves to the next; the run reports which provider was used.
- Approvals: `agent.run(..., approval_callback=...)` receives
  `(tool_name, args, approval_id)` and returns `True` only for the
  `write_changelog` call carrying the exact operator-approved content.
- Reviewer: `subagent(parent, instructions=..., tools=[read_commits],
  output_type=ReviewVerdict)` reuses the parent runtime with isolated
  child context.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt   # openminion==0.1.22
```

## Run

```bash
# Build the deterministic fixture repo first:
bash fixtures/sample-repo/build.sh

# Draft notes for v0.1.0..HEAD (interactive approval):
.venv/bin/python app.py ./fixtures/sample-repo/repo v0.1.0..HEAD

# Options:
#   --version-label v0.2.0     heading for the CHANGELOG entry
#   --provider openrouter      use only one provider rung
#   --no-reviewer              skip the reviewer subagent
#   --transcript run.log       append a timestamped run log (no secrets)
```

## API keys (environment only, never in files)

```bash
export OPENROUTER_API_KEY="..."            # primary (OpenRouter free models)
export OPENAI_COMPATIBLE_API_KEY="..."     # fallback rungs (100milionseek/Coarena)
```

On the author's machine the Secure Vault surrogate is picked up
automatically when the env var is unset — see
`releasescribe/providers.py`. Never paste a key into a file, the repo, a
log, or chat. Do not send secrets through these third-party proxies.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Unit tests cover: git reading + range validation, typed-output validation,
the write-approval gate (refused without approval, refused for tampered
content), provider config + fallback, session refinement, and the reviewer
(non-fatal on failure). They run with fake agents — no model, no network.

## Demo script

`demo.sh` runs the full flow end to end against the fixture repo
(draft → review → approve → `CHANGELOG.md` written) and prints what to
expect at each step. Pipe `approve` on stdin for a non-interactive run.

## Files

| Path | What it is |
|---|---|
| `app.py` | CLI: draft → show → refine/approve → write |
| `releasescribe/models.py` | `ReleaseNotes`, `CommitEntry`, `ReviewVerdict` |
| `releasescribe/gitops.py` | Pure git ops: read commits, render, write |
| `releasescribe/tools.py` | `@tool` wrappers + in-process approval gate |
| `releasescribe/providers.py` | Free-model fallback chain + config builder |
| `releasescribe/agent.py` | Orchestration: draft/refine/review/write |
| `tests/` | Unit tests (fake agents, no network) |
| `fixtures/sample-repo/` | Deterministic fixture repo builder |
| `BUILDING_BLOCKS.md` | What was tested, what works, bugs found |
| `WHAT_WE_LEARNED.md` | Product feedback for OpenMinion |

## Hackathon #5 submission notes

- Public repo, MIT. One reproducible example + demo video (see `demo.sh`).
- `BUILDING_BLOCKS.md` lists every building block exercised, what worked,
  and the bugs/rough edges found with reproduction steps.
- `WHAT_WE_LEARNED.md` holds actionable product feedback.
