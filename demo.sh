#!/usr/bin/env bash
# ReleaseScribe demo script — the exact commands for the 1–2 minute demo
# video, plus what to expect at each step.
#
# Prerequisites: venv with openminion==0.1.22 (see README Setup) and an LLM
# key in the environment (OPENROUTER_API_KEY, or the Secure Vault surrogate
# path on the author's machine). No key is ever printed or stored by this
# script.
#
# Usage: bash demo.sh
#   Runs: unit tests -> fixture build -> live agent run (refine + approve)
#   The live run writes CHANGELOG.md into the fixture repo and verifies it
#   byte-for-byte against the approved draft.
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then PY="$(command -v python3)"; fi

echo "=== 1. Unit tests (no model, no network) ==="
echo "Expect: 48 tests, OK"
"$PY" -m unittest discover -s tests

echo
echo "=== 2. Build the deterministic fixture repo ==="
echo "Expect: 6 commits, v0.1.0 tags the first; range v0.1.0..HEAD = 5 commits"
bash fixtures/sample-repo/build.sh

echo
echo "=== 3. Live run: draft -> review -> refine -> approve -> write ==="
echo "Expect:"
echo "  - draft via OpenRouter/Ernie (free); if it rate-limits, the chain"
echo "    falls back to 100milionseek automatically (watch the attempts)"
echo "  - reviewer verdict printed (may flag a non-structured reply;"
echo "    the draft is still shown and the operator decides)"
echo "  - refine shortens the summary to one sentence, in-session"
echo "  - approve writes CHANGELOG.md; the CLI then prints VERIFIED"
echo "    (file matches the approved draft byte-for-byte)"
printf 'refine perpendek summary menjadi satu kalimat\napprove\n' \
  | "$PY" app.py ./fixtures/sample-repo/repo v0.1.0..HEAD \
      --transcript /tmp/releasescribe-demo-transcript.txt

echo
echo "=== 4. The result ==="
echo "Expect: CHANGELOG.md with 2 features, 2 fixes, 1 breaking change,"
echo "all matching the 5 fixture commits (no hallucinations)."
cat fixtures/sample-repo/repo/CHANGELOG.md
