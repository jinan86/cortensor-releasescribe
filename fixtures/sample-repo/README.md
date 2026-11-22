# Sample fixture repo (ReleaseScribe)

This directory holds the fixture *builder*. The fixture repo itself lives
in `./repo/` and is **generated, not committed** (gitignored). Rebuild it
any time:

```bash
bash fixtures/sample-repo/build.sh
```

## Contents after build (`./repo/`)

6 commits, `v0.1.0` tags the first one. The range `v0.1.0..HEAD` contains
exactly these 5 commits:

| Commit | Expected bucket |
|---|---|
| `feat: add dark mode toggle` | features |
| `fix: correct total in checkout summary` | fixes |
| `feat: export report as CSV` | features |
| `fix: handle empty cart on checkout` | fixes |
| `feat!: remove legacy /v1 endpoint` | breaking |

Author/committer dates are fixed, so the SHAs are deterministic across
rebuilds on the same machine.
