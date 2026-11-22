"""Unit tests: provider chain config + env handling (no keys, no network)."""

import json
import os
import unittest
from pathlib import Path

from releasescribe import providers
from releasescribe.providers import ProviderSpec


def _spec(**overrides):
    base = dict(
        name="test/model",
        setup_id="openrouter",
        model="test/model:free",
        base_url="https://example.invalid/v1",
        api_key_env="RS_TEST_API_KEY",
        surrogate_credential="custom.test",
        max_tokens=512,
    )
    base.update(overrides)
    return ProviderSpec(**base)


class TestConfigBuilder(unittest.TestCase):
    def test_config_has_console_channel(self):
        cfg = providers.build_config_dict(_spec())
        self.assertEqual(
            cfg["agents"]["releasescribe"]["default_channel"], "console"
        )
        self.assertEqual(cfg["default_agent"], "releasescribe")

    def test_config_never_contains_a_key(self):
        cfg = providers.build_config_dict(_spec())
        blob = json.dumps(cfg)
        self.assertNotIn("sk-", blob)
        self.assertNotIn("hsurr:", blob)
        # Only the env var NAME is referenced, never a value.
        self.assertEqual(
            cfg["providers"]["openrouter"]["api_key_env"], "RS_TEST_API_KEY"
        )
        self.assertNotIn("api_key", cfg["providers"]["openrouter"])

    def test_ernie_needs_4096_max_tokens(self):
        ernie = providers.PROVIDER_CHAIN[0]
        self.assertEqual(ernie.model, "baidu/ernie-5.5-flash-preview:free")
        self.assertEqual(ernie.max_tokens, 4096)

    def test_chain_order(self):
        names = [s.name for s in providers.PROVIDER_CHAIN]
        self.assertEqual(
            names,
            [
                "openrouter/ernie-free",
                "100milionseek/deepseek-flash-free",
                "coarena/deepseek-flash",
            ],
        )

    def test_custom_endpoint_maps_to_openai_runtime_provider(self):
        """custom-openai-compatible is a setup preset, not a runtime key.

        The runtime factory rejects it ("Unknown provider"), so the config
        must use the "openai" runtime adapter with a base_url override.
        """
        spec = _spec(
            setup_id="custom-openai-compatible",
            runtime_provider="openai",
            base_url="https://100milionseek.top/v1",
            api_key_env="OPENAI_COMPATIBLE_API_KEY",
        )
        cfg = providers.build_config_dict(spec)
        self.assertEqual(cfg["agents"]["releasescribe"]["provider"], "openai")
        block = cfg["providers"]["openai"]
        self.assertEqual(block["base_url"], "https://100milionseek.top/v1")
        self.assertEqual(block["api_key_env"], "OPENAI_COMPATIBLE_API_KEY")
        self.assertNotIn("custom-openai-compatible", cfg["providers"])

    def test_chain_custom_rungs_use_openai_runtime(self):
        for spec in providers.PROVIDER_CHAIN[1:]:
            cfg = providers.build_config_dict(spec)
            self.assertEqual(
                cfg["agents"]["releasescribe"]["provider"], "openai"
            )

    def test_write_config_file_roundtrip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = providers.write_config_file(_spec(), tmp)
            loaded = json.loads(Path(path).read_text())
            self.assertEqual(loaded["providers"]["openrouter"]["model"],
                             "test/model:free")


class TestNoProxySanitizer(unittest.TestCase):
    def test_strips_bracketed_ipv6(self):
        os.environ["no_proxy"] = "localhost,127.0.0.1,::1,[::1],[fd8b::1]"
        os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1,[::1],[fd8b::1]"
        cleaned = providers.sanitize_no_proxy()
        self.assertEqual(cleaned, "localhost,127.0.0.1,::1")
        self.assertNotIn("[", os.environ["no_proxy"])
        self.assertNotIn("[", os.environ["NO_PROXY"])

    def test_leaves_clean_value_alone(self):
        os.environ["no_proxy"] = "localhost,127.0.0.1"
        os.environ["NO_PROXY"] = "localhost,127.0.0.1"
        self.assertEqual(
            providers.sanitize_no_proxy(), "localhost,127.0.0.1"
        )


class TestEnsureCredentials(unittest.TestCase):
    def test_prefers_existing_env_var(self):
        os.environ["RS_TEST_API_KEY"] = "literal-key-from-operator"
        try:
            self.assertTrue(providers.ensure_credentials(_spec()))
            # Must not overwrite an operator-provided key.
            self.assertEqual(
                os.environ["RS_TEST_API_KEY"], "literal-key-from-operator"
            )
        finally:
            del os.environ["RS_TEST_API_KEY"]

    def test_no_key_no_surrogate_returns_false(self):
        os.environ.pop("RS_TEST_API_KEY", None)
        spec = _spec(surrogate_credential="custom.does-not-exist")
        self.assertFalse(providers.ensure_credentials(spec))


if __name__ == "__main__":
    unittest.main()
