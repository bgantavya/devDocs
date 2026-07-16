"""Config precedence, validation, and key resolution.

No network and no os.environ mutation: `load_config` and `resolve_api_key` both
take an `env` mapping so these run in any order and in parallel.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devdocs.config import Config, load_config, resolve_api_key
from devdocs.errors import ConfigError


def write_config(body: str) -> Path:
    directory = Path(tempfile.mkdtemp())
    path = directory / "devdocs.toml"
    path.write_text(body, encoding="utf-8")
    return path


class TestPrecedence(unittest.TestCase):
    def test_defaults_when_nothing_supplied(self) -> None:
        config = load_config(config_file=None, env={})
        self.assertEqual(config.provider, "gemini")
        self.assertEqual(config.quality, "cheap")
        self.assertTrue(config.free_tier)
        self.assertEqual(config.origin_of("provider"), "default")

    def test_file_overrides_defaults(self) -> None:
        path = write_config('[devdocs]\nprovider = "anthropic"\nmax_cost = 2.5\n')
        config = load_config(config_file=path, env={})
        self.assertEqual(config.provider, "anthropic")
        self.assertEqual(config.max_cost, 2.5)
        self.assertEqual(config.origin_of("provider"), "file")

    def test_flag_overrides_file(self) -> None:
        path = write_config('[devdocs]\nprovider = "anthropic"\nquality = "high"\n')
        config = load_config({"provider": "gemini"}, config_file=path, env={})
        self.assertEqual(config.provider, "gemini")
        self.assertEqual(config.origin_of("provider"), "flag")
        # An unrelated flag must not disturb a configured value.
        self.assertEqual(config.quality, "high")
        self.assertEqual(config.origin_of("quality"), "file")

    def test_absent_flag_does_not_mask_file_value(self) -> None:
        path = write_config('[devdocs]\nquality = "balanced"\n')
        config = load_config({"quality": None}, config_file=path, env={})
        self.assertEqual(config.quality, "balanced")

    def test_bare_table_without_devdocs_section(self) -> None:
        path = write_config('provider = "anthropic"\n')
        config = load_config(config_file=path, env={})
        self.assertEqual(config.provider, "anthropic")

    def test_hyphenated_keys_map_to_underscores(self) -> None:
        path = write_config("[devdocs]\nmax-cost = 3.0\nfree-tier = false\n")
        config = load_config(config_file=path, env={})
        self.assertEqual(config.max_cost, 3.0)
        self.assertFalse(config.free_tier)

    def test_comma_string_becomes_tuple(self) -> None:
        path = write_config('[devdocs]\nexclude = "vendor, build , "\n')
        self.assertEqual(load_config(config_file=path, env={}).exclude,
                         ("vendor", "build"))


class TestValidation(unittest.TestCase):
    def test_rejects_unknown_provider(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"provider": "openai"}, config_file=None, env={})

    def test_rejects_unknown_quality(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"quality": "ultra"}, config_file=None, env={})

    def test_rejects_unknown_format(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"formats": ["xml"]}, config_file=None, env={})

    def test_rejects_zero_concurrency(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"concurrency": 0}, config_file=None, env={})

    def test_rejects_negative_cost(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"max_cost": -1.0}, config_file=None, env={})

    def test_rejects_a_cost_that_is_not_a_number(self) -> None:
        # Regression: TOML, the flag and the environment all accept nan and
        # inf, which passed validation and crashed Budget() with a raw error.
        for value in ("nan", "inf", "-inf"):
            path = write_config(f"max_cost = {value}\n")
            with self.subTest(file=value), self.assertRaises(ConfigError):
                load_config(config_file=path, env={})
            with self.subTest(env=value), self.assertRaises(ConfigError):
                load_config(config_file=None, env={"DEVDOCS_MAX_COST": value})
            with self.subTest(flag=value), self.assertRaises(ConfigError):
                load_config({"max_cost": float(value)}, config_file=None, env={})

    def test_rejects_negative_requests(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"max_requests": -1}, config_file=None, env={})

    def test_unknown_option_names_the_valid_ones(self) -> None:
        path = write_config("[devdocs]\nprovder = 1\n")
        with self.assertRaises(ConfigError) as caught:
            load_config(config_file=path, env={})
        self.assertIn("provider", str(caught.exception))

    def test_malformed_toml_names_the_file(self) -> None:
        path = write_config("[devdocs\nprovider =\n")
        with self.assertRaises(ConfigError) as caught:
            load_config(config_file=path, env={})
        self.assertIn(str(path), str(caught.exception))

    def test_wrong_type_is_rejected(self) -> None:
        path = write_config('[devdocs]\nconcurrency = "four"\n')
        with self.assertRaises(ConfigError):
            load_config(config_file=path, env={})


class TestEnvironment(unittest.TestCase):
    def test_env_overrides_file_and_flag_overrides_env(self) -> None:
        path = write_config('[devdocs]\nquality = "high"\nmax_cost = 1.0\n')
        env = {"DEVDOCS_QUALITY": "balanced", "DEVDOCS_MAX_COST": "2.5"}
        config = load_config({"max_cost": 0.1}, config_file=path, env=env)
        self.assertEqual(config.quality, "balanced")
        self.assertEqual(config.origin_of("quality"), "env")
        self.assertEqual(config.max_cost, 0.1)
        self.assertEqual(config.origin_of("max_cost"), "flag")

    def test_env_values_are_typed(self) -> None:
        env = {
            "DEVDOCS_FREE_TIER": "off",
            "DEVDOCS_CONCURRENCY": "8",
            "DEVDOCS_EXCLUDE": "vendor, dist",
            "DEVDOCS_PROVIDER": "anthropic-sdk",
        }
        config = load_config(config_file=None, env=env)
        self.assertFalse(config.free_tier)
        self.assertEqual(config.concurrency, 8)
        self.assertEqual(config.exclude, ("vendor", "dist"))
        self.assertEqual(config.provider, "anthropic-sdk")

    def test_bad_env_values_name_the_variable(self) -> None:
        for name, value in (("DEVDOCS_FREE_TIER", "maybe"),
                            ("DEVDOCS_CONCURRENCY", "four")):
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                load_config(config_file=None, env={name: value})
            self.assertIn(name, str(caught.exception))

    def test_blank_env_values_are_ignored(self) -> None:
        config = load_config(config_file=None, env={"DEVDOCS_QUALITY": "  "})
        self.assertEqual(config.origin_of("quality"), "default")

    def test_the_key_never_enters_config_from_env(self) -> None:
        config = load_config(config_file=None, env={"DEVDOCS_API_KEY": "secret"})
        self.assertEqual(config.api_key, "")


class TestKeyResolution(unittest.TestCase):
    def test_sdk_provider_reads_the_anthropic_variable(self) -> None:
        key, source = resolve_api_key(
            Config(provider="anthropic-sdk"), env={"ANTHROPIC_API_KEY": "ak"}
        )
        self.assertEqual((key, source.detail), ("ak", "ANTHROPIC_API_KEY"))

    def test_flag_wins_and_is_stripped(self) -> None:
        key, source = resolve_api_key(Config(), explicit="  k  ", env={})
        self.assertEqual(key, "k")
        self.assertEqual(source.origin, "flag")

    def test_env_is_searched_in_order(self) -> None:
        env = {"GEMINI_API_KEY": "second", "DEVDOCS_API_KEY": "first"}
        key, source = resolve_api_key(Config(), env=env)
        self.assertEqual(key, "first")
        self.assertEqual(source.detail, "DEVDOCS_API_KEY")

    def test_provider_picks_its_own_variable(self) -> None:
        env = {"ANTHROPIC_API_KEY": "ak", "GEMINI_API_KEY": "gk"}
        key, _ = resolve_api_key(Config(provider="anthropic"), env=env)
        self.assertEqual(key, "ak")

    def test_config_file_key_is_used_before_demo(self) -> None:
        key, source = resolve_api_key(Config(api_key="fk"), env={}, demo_key="demo")
        self.assertEqual(key, "fk")
        self.assertEqual(source.origin, "file")

    def test_demo_key_is_last_and_flagged_shared(self) -> None:
        key, source = resolve_api_key(Config(), env={}, demo_key="demo")
        self.assertEqual(key, "demo")
        self.assertTrue(source.shared)

    def test_no_key_anywhere_explains_how_to_supply_one(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            resolve_api_key(Config(), env={})
        self.assertIn("GEMINI_API_KEY", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
