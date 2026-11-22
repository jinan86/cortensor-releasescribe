# Building Blocks Tested

OpenMinion `0.1.22`, Python 3.12.3, tested 22 Nov 2026. Every claim below was
observed in a real run (live model unless marked "unit test"). The agent is
**ReleaseScribe** (this repo): it reads git commits, drafts typed release
notes, refines them in-session, and writes `CHANGELOG.md` only after
operator approval.

## What works

### 1. Decorated tools + `forced_tools` — WORKS
- `@tool(min_scope="READ_ONLY", idempotent=True)` for `read_commits`,
  `@tool(min_scope="WRITE_SAFE", idempotent=False)` for `write_changelog`.
- `forced_tools=["read_commits"]` makes the first turn call the tool instead
  of answering from memory. Observed in all live runs: the model called
  `read_commits` with the exact repo path + range, then drafted from the
  returned commits.
- Tool results round-trip as JSON strings and the model uses them correctly
  (5/5 fixture commits categorized correctly in every live run, zero
  hallucinations in the draft itself).

### 2. Typed output (Pydantic `output_type=`) — WORKS, with retries
- `output_type=ReleaseNotes` returns a validated `ReleaseNotes` object on
  `result.output`. Invalid replies raise `AgentOutputValidationError` with
  `raw_text` preserved (used for the reviewer fallback).
- Free models often do NOT return JSON on the first try: Ernie returned
  Markdown prose once; the reviewer subagent returned plain `ok` once and a
  wrong-shaped object twice. ReleaseScribe retries once in-session with an
  explicit "reply with ONLY the JSON object" message; the draft retry
  succeeded every time it was needed.
- Note: the SDK coerces output prompt-side (schema guidance in messages),
  not via a hard `response_format: json_schema` enforcement — so compliance
  depends on the model. See `WHAT_WE_LEARNED.md`.

### 3. Sessions — WORKS within one `Agent` instance; NOT across instances
- One `session_id` per repo+range; draft → refine on the **same live
  `Agent` instance** shares history (refine "perpendek summary" worked and
  kept all items).
- **Does NOT carry across `Agent` instances.** Probe, 22 Nov 2026: draft
  with Agent A (session `releasescribe-...`, shared config dir/state DB),
  then ask Agent B — same `session_id`, same config dir — to quote the
  draft summary. Reply: "I don't have a draft to quote — no release notes
  have been written in this conversation." ReleaseScribe therefore keeps
  one live draft agent per run and passes the current draft explicitly in
  refine messages (defense in depth).
- Reproduce: `/tmp/rs_probe_session.py` pattern — two `Agent(...)` with
  identical `session_id` + `config_path`, second asks about the first's
  turn. (Script not committed; the finding is recorded here and in
  `releasescribe/agent.py`.)

### 4. Approvals (`approval_callback`) — WORKS
- `agent.run(..., approval_callback=...)` **is consulted** for the
  `WRITE_SAFE` tool: observed invocation `tool=write_changelog,
  approved=True` in the live write turns. Returning `False` denies the
  call (unit-tested via fakes).
- The `thread_id` resume path from the quickstart was not needed: the
  callback path covers this workflow. Not tested: pausing mid-turn for an
  interactive yes/no and resuming with `thread_id`.
- Defense in depth: the tool itself also refuses unapproved content
  (`PermissionError`, unit-tested: refused without approval, refused for
  tampered content, allowed for exact approved content).

### 5. Provider fallback + error handling — WORKS (live)
- `ProviderError` / `TurnTimeoutError` caught per rung; chain
  OpenRouter → 100milionseek → Coarena.
- Observed live, 22 Nov 2026: OpenRouter failed with `RATE_LIMITED`
  ("free-models-per-day") and the run continued on 100milionseek, which
  drafted and wrote successfully. Earlier, a plain-text probe through
  OpenRouter/Ernie succeeded (`OK`).
- 100milionseek works through the SDK's httpx client (no Cloudflare block
  observed on 22 Nov). Coarena rung configured but never reached live (the
  first two rungs sufficed in every run); its wiring is unit-tested only.
- Error messages are truncated to 200 chars in our attempt log; the SDK's
  own message already truncates the provider detail.

### 6. Subagent (reviewer) — RUNS, typed output UNRELIABLE
- `subagent(parent, instructions=..., tools=[read_commits],
  output_type=ReviewVerdict)` constructs and runs; it does call
  `read_commits`.
- But its typed output is unreliable with these free models: across 3 live
  runs it returned (a) plain text `ok` (correct verdict, wrong format —
  recovered by our raw-text fallback), (b) a `ReleaseNotes`-shaped object
  (parent schema leaking into the child), (c) a garbled non-verdict
  payload. Our reviewer is therefore best-effort and non-fatal: a failed
  review is reported to the operator, never blocks the draft.
- No hallucination was ever found in a draft by any reviewer reply; the
  drafts themselves were correct in all runs.

### 7. Echo provider — WORKS (credential-free bring-up)
- `config init --provider echo` + `openminion run "hello"` works (Phase 0).
  Echo proves CLI + runtime + config + storage only; it cannot prove tool
  use, typed output, sessions, or approvals.

### 8. `APIRuntime` / lower-level runtime — NOT USED
- The `Agent` facade covered everything. Not tested.

### 9. Cortensor Portal / Gemma4 — NOT TESTED
- No access arranged; docs mark Portal experimental. Not required for this
  entry and not penalized per the hackathon rules.

## Bugs / rough edges found (with reproduction)

### B1. `custom-openai-compatible` is a setup preset, not a runtime provider
- **Expected:** config with `"provider": "custom-openai-compatible"` (as in
  the Providers docs + Phase 0 config pattern) runs against the custom base
  URL.
- **Actual:** `ProviderError: Unknown provider 'custom-openai-compatible'.
  Supported: anthropic, cerebras, claude, cortensor, echo, groq, ollama,
  openai, openrouter` — the runtime factory has no branch for it.
- **Reproduce:** any `Agent(config_path=...)` whose `agents.<id>.provider`
  is `custom-openai-compatible`.
- **Workaround (used here):** configure the agent as provider `"openai"`
  with `base_url` + `api_key_env` overrides in the `openai` provider block
  — exactly what `openminion setup --provider custom-openai-compatible
  --base-url ... --model ...` writes. `releasescribe/providers.py`
  (`runtime_provider`) implements this mapping.

### B2. Programmatic runs need `default_channel: "console"` (undocumented)
- **Expected:** the quickstart's `Agent(...)` example works with a minimal
  hand-written config.
- **Actual:** every `Agent.run` fails with `RuntimeError: inbound
  authenticity denied (reason=invalid_channel, mode=warn)` until the agent
  profile sets `"default_channel": "console"`.
- **Reproduce:** build a minimal `agents.json` without `default_channel`
  and call `Agent(config_path=...).run("hello")`.
- **Workaround:** `releasescribe/providers.py:build_config_dict` always
  sets it.

### B3. `@tool` silently accepts invalid `min_scope` values
- **Expected:** `min_scope="WRITE"` (a natural guess; the plan originally
  said WRITE) is rejected or documented.
- **Actual:** the decorator accepts it with no error; the valid set is the
  `Literal["READ_ONLY", "WRITE_SAFE", "POWER_USER", "UI_AUTOMATION"]`.
  Behavior of an out-of-range scope at policy time is unknown.
- **Workaround:** this repo uses `WRITE_SAFE` for the write tool.

### B4. httpx chokes on bracketed IPv6 entries in `no_proxy`
- **Expected:** SDK HTTP calls work with the VM's default proxy env.
- **Actual:** `InvalidURL: Invalid port: ':1]'` on every provider request
  (httpx parses `[::1]` in `no_proxy` as host with port `:1]`).
- **Workaround:** `releasescribe/providers.py:sanitize_no_proxy()` strips
  bracketed entries at process start. Environment quirk, not an SDK bug —
  but any httpx-based SDK hits it.

### B5. Session history does not restore across `Agent` instances
- See §3 above. The quickstart's "Continue a session" example uses one
  instance; nothing warns that a second instance with the same
  `session_id` starts blank. Workaround in this repo: keep one live agent
  per run + pass state explicitly in messages.

### B6. Free-model rate limits are per-day and opaque
- OpenRouter free tier returned `RATE_LIMITED: ... free-models-per-day`
  after a handful of runs in one day. The SDK surfaces the provider's
  message, but the quota state (remaining/limit/reset) is not exposed.
  Mitigation is the fallback chain, not a fix.

## Model-behavior notes (not SDK bugs)
- **False narration:** in one live write turn the model claimed the content
  field had been empty and that it "filled it in from inferred changes" —
  while the SDK approval callback had approved the exact content
  (`approved=True`) and the file matched the approved draft byte-for-byte.
  ReleaseScribe now verifies the file on disk after every write and
  reports VERIFIED/MISMATCH instead of trusting the model's closing text.
- **Reviewer unreliability:** see §6. Drafts were correct in all runs; the
  reviewer is the weak link, not the drafter.
