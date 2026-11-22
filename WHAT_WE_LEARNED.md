# What We Learned

Feedback for the OpenMinion team, from building ReleaseScribe on SDK
`0.1.22`. Ordered by how much it would have helped a first-time builder.

## 1. Tell programmatic users about `default_channel`
The quickstart's `Agent(...)` example fails out of the box with
`inbound authenticity denied (reason=invalid_channel, mode=warn)` unless
the config sets `"default_channel": "console"`. The error gives no hint
about the fix. Either default the channel to `console` for local
programmatic runs, or mention it in the quickstart next to the `Agent`
example. (See `BUILDING_BLOCKS.md` B2.)

## 2. Make setup presets and runtime providers agree
`custom-openai-compatible` exists in `setup --list-providers` and in the
Providers docs, but the runtime factory rejects it as unknown. A builder
who follows the docs hits a wall at the first real run. Either add the
runtime branch or have `setup` document that the preset maps to the
`openai` adapter. (B1.)

## 3. Validate `@tool` scopes at decoration time
`min_scope="WRITE"` is silently accepted although the valid set is
`READ_ONLY | WRITE_SAFE | POWER_USER | UI_AUTOMATION`. A typo or a natural
guess fails late (or never, silently). Raising at decoration time would
catch it immediately. (B3.)

## 4. Structured output needs a real enforcement path
Typed output currently works by prompting the model to return JSON. Free
models comply only sometimes — our runs needed a retry loop, and the
reviewer subagent never once returned the right schema on the first try.
A `response_format: json_schema` path (where the provider supports it),
or a built-in "retry once as JSON-only" helper, would make `output_type`
trustworthy instead of advisory. At minimum, document that it is
prompt-based so builders plan retries.

## 5. Sessions: say what `session_id` actually restores
A second `Agent` with the same `session_id` and the same config/state dir
starts blank in our probe — continuity only holds within one live
instance. Either make cross-instance restore work, or document the
boundary clearly ("keep the Agent alive for multi-turn work"). Builders
currently discover this by losing context mid-demo. (B5.)

## 6. Surface quota state on rate-limit errors
Free-tier `RATE_LIMITED` errors don't say how much quota is left or when
it resets. Even a passthrough of the provider's `Retry-After` /
rate-limit headers in `ProviderError.details` would let apps back off
intelligently instead of blindly failing over.

## 7. Subagents inherit too much of the parent's shape
Our reviewer subagent (own instructions, own `output_type=ReviewVerdict`)
repeatedly answered with the parent's `ReleaseNotes` shape. If the child
is meant to be independent, its output contract should be enforced
independently of the parent's; if it inherits by design, say so in the
`subagent` docs.

## 8. Approval observability
`approval_callback` works well. What is missing is any record that it was
consulted: we had to instrument it ourselves to prove the policy asked.
A small run-stat (e.g. `result.stats["approvals_requested"]`) would let
apps show the operator "the runtime asked, you approved" instead of
guessing.

## What worked well (keep it)
- The `Agent` facade + `tool` decorator + `forced_tools` got a tool-using
  agent running in minutes once the provider was configured.
- `AgentOutputValidationError.raw_text` is exactly the right escape hatch —
  it let us build the reviewer raw-text fallback.
- `ProviderError.code` (`RATE_LIMITED`, etc.) made the fallback chain easy
  to write correctly.
- The echo provider is a genuinely useful credential-free smoke test.
