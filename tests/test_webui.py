"""网页界面：服务层与 HTTP 端点的测试（都不需要真的开浏览器）。"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_mvp.llm.base import LLMError
from workflow_mvp.webui import ReviewService, build_server, example_code, load_page, provider_status


def wait_done(run, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while run.status == "running" and time.time() < deadline:
        time.sleep(0.05)
    if run.status == "running":
        raise AssertionError("审查超时未结束")


class WebPageTest(unittest.TestCase):
    def test_page_has_key_parts(self) -> None:
        page = load_page()
        self.assertIn("代码审查工作流", page)
        self.assertIn("/api/review", page)
        self.assertIn("严重度", page)

    def test_example_has_known_problems(self) -> None:
        example = example_code()
        self.assertIn("eval(", example)
        self.assertIn("== None", example)
        self.assertTrue(example.strip())


class ServiceTest(unittest.TestCase):
    def test_review_runs_and_reports(self) -> None:
        service = ReviewService()
        run = service.start(example_code(), "order_price.py", max_rounds=2)
        wait_done(run)
        self.assertEqual(run.status, "completed")
        payload = run.payload
        self.assertTrue(payload["findings"])
        self.assertTrue(payload["steps"])
        self.assertTrue(payload["diff"])
        self.assertTrue(any(f["severity"] == "high" for f in payload["findings"]))
        self.assertTrue(any(m["kind"] in ("result", "feedback") for m in payload["messages"]))
        self.assertEqual(payload["steps"][0]["node"], "coder")

    def test_empty_code_rejected(self) -> None:
        service = ReviewService()
        with self.assertRaises(ValueError):
            service.start("   \n", "x.py")

    def test_events_are_recorded(self) -> None:
        service = ReviewService()
        run = service.start(example_code(), "order_price.py", max_rounds=1)
        wait_done(run)
        levels = {event["level"] for event in run.events}
        self.assertIn("step", levels)
        self.assertIn("done", levels)

    def test_provider_status_shape(self) -> None:
        status = provider_status()
        for key in ("provider", "model", "key_configured", "env_paths"):
            self.assertIn(key, status)
        self.assertTrue(status["env_paths"])

    def test_model_without_key_falls_back(self) -> None:
        service = ReviewService()
        with mock.patch("workflow_mvp.webui.create_provider", side_effect=LLMError("缺少 API Key")):
            run = service.start(example_code(), "x.py", max_rounds=1, use_model=True)
            wait_done(run)
        self.assertEqual(run.status, "completed")
        self.assertTrue(any(event["level"] == "warn" for event in run.events))
        self.assertTrue(run.payload["findings"])

    def test_status_payload_is_json_safe(self) -> None:
        service = ReviewService()
        run = service.start(example_code(), "order_price.py", max_rounds=1)
        wait_done(run)
        json.dumps(run.to_dict(), ensure_ascii=False)


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.httpd = build_server(port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def _post(self, path: str, payload: dict) -> tuple[int, str]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # 4xx 也要能读到错误体
            return exc.code, exc.read().decode("utf-8")

    def test_index_page(self) -> None:
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("代码审查工作流", body)

    def test_index_with_query_string(self) -> None:
        status, body = self._get("/?demo=1")
        self.assertEqual(status, 200)
        self.assertIn("代码审查工作流", body)

    def test_config_endpoint(self) -> None:
        status, body = self._get("/api/config")
        self.assertEqual(status, 200)
        cfg = json.loads(body)
        self.assertIn("key_configured", cfg)
        self.assertIn("provider", cfg)

    def test_health_and_example(self) -> None:
        self.assertEqual(json.loads(self._get("/api/health")[1]), {"ok": True})
        example = json.loads(self._get("/api/example")[1])
        self.assertIn("eval(", example["code"])

    def test_review_roundtrip(self) -> None:
        status, body = self._post("/api/review", {"code": example_code(), "filename": "x.py", "max_rounds": 1})
        self.assertEqual(status, 200)
        run_id = json.loads(body)["run_id"]
        for _ in range(200):
            payload = json.loads(self._get(f"/api/status?id={run_id}")[1])
            if payload["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["payload"]["findings"])

    def test_bad_request(self) -> None:
        status, body = self._post("/api/review", {"code": ""})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))


if __name__ == "__main__":
    unittest.main()
