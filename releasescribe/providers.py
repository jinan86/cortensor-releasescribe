"""Provider chain for ReleaseScribe (free models only).

Order (mirrors the chain proven in the Sentinel project):

1. OpenRouter preset ``openrouter`` — ``baidu/ernie-5.5-flash-preview:free``.
   Ernie is a reasoning model: it needs ``max_tokens`` 4096 or its JSON
   verdict gets truncated.
2. 100milionseek via ``custom-openai-compatible`` —
   ``deepseek-v4.1-flash-free`` at https://100milionseek.top/v1.
3. Coarena via ``custom-openai-compatible`` —
   ``deepseek-v4-1-flash-1017`` at https://coarena.ai/api/v1.

API keys are NEVER stored in files, the repo, logs, or chat. They reach the
process in one of two ways, in this order:

* an env var the operator already exported (``OPENROUTER_API_KEY`` /
  ``OPENAI_COMPATIBLE_API_KEY``), or
* a Secure Vault surrogate (``hsurr:...``) fetched through the documented
  ``dynamic_credentials`` helper and placed in the env var the SDK reads.
  The surrogate is a scoped reference, not the key; it is never printed.
  The egress layer swaps it for the real credential on the approved
  request to the registered host. If the helper is unavailable (e.g. a
  machine without the Muse connector runtime), the provider is skipped
  with a clear message instead of failing obscurely.

Two environment quirks discovered while wiring this up (see
BUILDING_BLOCKS.md):

* OpenMinion agent profiles need ``"default_channel": "console"`` in
  ``agents.json``; without it every programmatic ``Agent.run`` fails with
  ``inbound authenticity denied (reason=invalid_channel, mode=warn)``.
* The SDK's HTTP client (httpx) raises ``InvalidURL: Invalid port`` on
  bracketed IPv6 entries such as ``[::1]`` in ``no_proxy``. The chain
  sanitiser below strips those entries before any agent is built.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# The dynamic-credentials helper lives in the Muse skill tree. It is
# optional: without it, only pre-exported env vars can authenticate.
_DYNAMIC_CREDENTIALS_PATH = "/opt/muse/skills/custom-connector-setup/bin"


@dataclass(frozen=True)
class ProviderSpec:
    """One rung of the provider fallback chain."""

    name: str  # human label, e.g. "openrouter/ernie-free"
    setup_id: str  # OpenMinion setup preset id (for display / setup CLI)
    model: str
    base_url: str | None  # None for presets with a built-in endpoint
    api_key_env: str  # env var the SDK reads for this provider
    surrogate_credential: str  # Secure Vault credential name, e.g. custom.openrouter
    max_tokens: int
    temperature: float = 0.2
    timeout_seconds: int = 90
    # Provider key the RUNTIME factory knows. Custom OpenAI-compatible
    # endpoints are a SETUP preset only: the runtime factory rejects
    # "custom-openai-compatible" ("Unknown provider"), so they must be
    # configured as the "openai" runtime adapter with a base_url override
    # (exactly what `openminion setup --provider custom-openai-compatible`
    # writes). See BUILDING_BLOCKS.md.
    runtime_provider: str = ""


PROVIDER_CHAIN: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="openrouter/ernie-free",
        setup_id="openrouter",
        model="baidu/ernie-5.5-flash-preview:free",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        surrogate_credential="custom.openrouter",
        max_tokens=4096,
    ),
    ProviderSpec(
        name="100milionseek/deepseek-flash-free",
        setup_id="custom-openai-compatible",
        model="deepseek-v4.1-flash-free",
        base_url="https://100milionseek.top/v1",
        api_key_env="OPENAI_COMPATIBLE_API_KEY",
        surrogate_credential="custom.100milionseek",
        max_tokens=1024,
        runtime_provider="openai",
    ),
    ProviderSpec(
        name="coarena/deepseek-flash",
        setup_id="custom-openai-compatible",
        model="deepseek-v4-1-flash-1017",
        base_url="https://coarena.ai/api/v1",
        api_key_env="OPENAI_COMPATIBLE_API_KEY",
        surrogate_credential="custom.coarena",
        max_tokens=1024,
        runtime_provider="openai",
    ),
)


def sanitize_no_proxy() -> str:
    """Strip bracketed IPv6 entries (``[::1]``) from no_proxy/NO_PROXY.

    httpx — the HTTP client inside the OpenMinion SDK — parses ``[::1]`` as
    a host with port ``:1]`` and raises ``InvalidURL`` on every request.
    Plain hosts (``localhost``, ``::1``, ``127.0.0.1``) are kept, so local
    bypass behaviour is unchanged. Returns the cleaned value.
    """
    cleaned = ""
    for key in ("no_proxy", "NO_PROXY"):
        raw = os.environ.get(key, "")
        if not raw:
            continue
        cleaned = ",".join(
            part
            for part in raw.split(",")
            if not (part.startswith("[") and part.endswith("]"))
        )
        os.environ[key] = cleaned
    # Return the lowercase value when present (the one most clients read
    # first); fall back to the uppercase value otherwise.
    return os.environ.get("no_proxy") or os.environ.get("NO_PROXY", "")


def runtime_provider_for(spec: ProviderSpec) -> str:
    """Provider key the runtime factory accepts for this spec."""
    return spec.runtime_provider or spec.setup_id


def build_config_dict(spec: ProviderSpec) -> dict:
    """Build the ``agents.json`` config for one provider.

    The key itself is never written here: ``api_key_env`` names the env var
    the SDK reads at runtime. ``default_channel: console`` is required for
    programmatic runs (see module docstring).
    """
    provider_key = runtime_provider_for(spec)
    provider_block: dict = {
        "model": spec.model,
        "api_key_env": spec.api_key_env,
        "temperature": spec.temperature,
        "timeout_seconds": spec.timeout_seconds,
        "max_tokens": spec.max_tokens,
        "tool_call_strategy": "hybrid",
    }
    if spec.base_url:
        provider_block["base_url"] = spec.base_url
    return {
        "default_agent": "releasescribe",
        "agents": {
            "releasescribe": {
                "name": "releasescribe",
                "provider": provider_key,
                "default_channel": "console",
            }
        },
        "providers": {provider_key: provider_block},
    }


def write_config_file(spec: ProviderSpec, directory: str | Path) -> Path:
    """Write the provider config into ``directory`` and return its path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "agents.json"
    path.write_text(json.dumps(build_config_dict(spec), indent=2), encoding="utf-8")
    return path


def _fetch_surrogate(credential_name: str) -> str | None:
    """Fetch an ``hsurr:...`` surrogate for a registered credential.

    Returns None when the helper is unavailable or the credential has no
    surrogate. Never prints or logs the surrogate value.
    """
    try:
        if _DYNAMIC_CREDENTIALS_PATH not in sys.path:
            sys.path.insert(0, _DYNAMIC_CREDENTIALS_PATH)
        from dynamic_credentials import dynamic_credential_entry
    except Exception:
        return None
    try:
        entry = dynamic_credential_entry(credential_name)
        surrogate = str(entry.get("surrogate", "")).strip()
        return surrogate if surrogate.startswith("hsurr:") else None
    except Exception:
        return None


def ensure_credentials(spec: ProviderSpec) -> bool:
    """Make sure the SDK can authenticate for ``spec``.

    Returns True when the env var is set (either it already was, or a
    surrogate was just placed there). Returns False when neither a
    pre-exported key nor a surrogate is available — the caller should skip
    this provider and try the next rung.
    """
    if os.environ.get(spec.api_key_env):
        return True
    surrogate = _fetch_surrogate(spec.surrogate_credential)
    if surrogate:
        os.environ[spec.api_key_env] = surrogate
        return True
    return False
