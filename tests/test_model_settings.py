"""模型设置：切换厂商、掩码、持久化、连通性测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_mvp.llm.base import LLMError
from workflow_mvp.model_settings import ModelSettings, mask_key


class MaskKeyTest(unittest.TestCase):
    def test_mask(self) -> None:
        self.assertEqual(mask_key(""), "")
        self.assertEqual(mask_key("short"), "***")
        self.assertEqual(mask_key("sk-abcdef123456"), "sk-a***56")


class UpdateTest(unittest.TestCase):
    def test_switch_provider_and_model(self) -> None:
        settings = ModelSettings(provider="deepseek", api_key="sk-old-123456")
        settings.update({"provider": "qwen", "model": "qwen-max", "base_url": "https://example.com/v1"})
        self.assertEqual(settings.provider, "qwen")
        self.assertEqual(settings.model, "qwen-max")
        self.assertEqual(settings.base_url, "https://example.com/v1")

    def test_unknown_provider_rejected(self) -> None:
        settings = ModelSettings()
        with self.assertRaises(ValueError):
            settings.update({"provider": "not-a-provider"})

    def test_custom_requires_base_url(self) -> None:
        settings = ModelSettings()
        with self.assertRaises(ValueError):
            settings.update({"provider": "custom", "base_url": ""})
        settings.update({"provider": "custom", "base_url": "https://api.example.com/v1"})
        self.assertEqual(settings.provider, "custom")

    def test_empty_key_clears(self) -> None:
        settings = ModelSettings(provider="deepseek", api_key="sk-abcdef123456")
        settings.update({"api_key": ""})
        self.assertEqual(settings.api_key, "")

    def test_none_key_keeps_existing(self) -> None:
        settings = ModelSettings(provider="deepseek", api_key="sk-abcdef123456")
        settings.update({"model": "deepseek-chat"})
        self.assertEqual(settings.api_key, "sk-abcdef123456")


class PublicTest(unittest.TestCase):
    def test_key_is_masked(self) -> None:
        settings = ModelSettings(provider="deepseek", api_key="sk-abcdef123456")
        payload = settings.public()
        self.assertTrue(payload["key_configured"])
        self.assertEqual(payload["key_hint"], "sk-a***56")
        self.assertNotIn("sk-abcdef123456", str(payload))

    def test_provider_choices(self) -> None:
        payload = ModelSettings().public()
        ids = [item["id"] for item in payload["providers"]]
        for expected in ("deepseek", "qwen", "openai", "moonshot", "ollama", "custom"):
            self.assertIn(expected, ids)


class BuildTest(unittest.TestCase):
    def test_custom_provider_builds(self) -> None:
        settings = ModelSettings(provider="custom", base_url="https://api.example.com/v1", api_key="k", model="m")
        provider = settings.build()
        self.assertEqual(provider.base_url, "https://api.example.com/v1")
        self.assertEqual(provider.model, "m")

    def test_missing_key_reports_clearly(self) -> None:
        settings = ModelSettings(provider="deepseek", api_key="")
        result = settings.test_connection()
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "config")
        self.assertIn("DEEPSEEK_API_KEY", result["error"])


class PersistTest(unittest.TestCase):
    def test_persist_updates_only_related_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# 注释保留\nPROVIDER=deepseek\nOTHER=keep\nDEEPSEEK_API_KEY=old\n", encoding="utf-8")
            settings = ModelSettings(provider="deepseek", model="deepseek-chat", api_key="sk-new-123456")
            settings.persist(env)
            text = env.read_text(encoding="utf-8")
            self.assertIn("# 注释保留", text)
            self.assertIn("OTHER=keep", text)
            self.assertIn("DEEPSEEK_API_KEY=sk-new-123456", text)
            self.assertIn("MODEL=deepseek-chat", text)
            self.assertNotIn("DEEPSEEK_API_KEY=old", text)

    def test_persist_custom_key_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            settings = ModelSettings(provider="custom", base_url="https://x/v1", api_key="sk-custom-1")
            settings.persist(env)
            text = env.read_text(encoding="utf-8")
            self.assertIn("CUSTOM_API_KEY=sk-custom-1", text)
            self.assertIn("BASE_URL=https://x/v1", text)


if __name__ == "__main__":
    unittest.main()
