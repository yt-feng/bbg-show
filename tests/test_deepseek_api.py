from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import deepseek_api


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def response_with_content(payload: dict) -> FakeResponse:
    return FakeResponse(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(payload, ensure_ascii=False),
                    }
                }
            ]
        }
    )


class DeepSeekAPITests(unittest.TestCase):
    def test_v4_flash_non_thinking_payload_uses_chat_completions(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                deepseek_api,
                "urlopen",
                return_value=response_with_content({"clips": []}),
            ) as mocked_urlopen,
        ):
            result = deepseek_api.request_deepseek_json(
                "secret",
                "system",
                "user",
                temperature=0.2,
                attempts=1,
            )

        self.assertEqual(result, {"clips": []})
        request = mocked_urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["temperature"], 0.2)

    def test_model_can_be_overridden(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": "deepseek-v4-pro"}):
            payload = deepseek_api.build_json_chat_payload(
                "system",
                "user",
                temperature=0.1,
            )
        self.assertEqual(payload["model"], "deepseek-v4-pro")

    def test_non_retryable_http_error_includes_response_body(self) -> None:
        error = HTTPError(
            deepseek_api.DEEPSEEK_URL,
            400,
            "Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"Model Not Exist"}}'),
        )
        with (
            patch.object(deepseek_api, "urlopen", side_effect=error) as mocked_urlopen,
            patch.object(deepseek_api.time, "sleep") as mocked_sleep,
            self.assertRaises(deepseek_api.DeepSeekAPIError) as raised,
        ):
            deepseek_api.request_deepseek_json(
                "secret",
                "system",
                "user",
                attempts=3,
            )

        self.assertIn("HTTP 400: Bad Request", str(raised.exception))
        self.assertIn("Model Not Exist", str(raised.exception))
        self.assertEqual(mocked_urlopen.call_count, 1)
        mocked_sleep.assert_not_called()

    def test_retryable_server_error_retries_then_returns_json(self) -> None:
        error = HTTPError(
            deepseek_api.DEEPSEEK_URL,
            503,
            "Service Unavailable",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"try again"}}'),
        )
        with (
            patch.object(
                deepseek_api,
                "urlopen",
                side_effect=[error, response_with_content({"ok": True})],
            ) as mocked_urlopen,
            patch.object(deepseek_api.time, "sleep") as mocked_sleep,
        ):
            result = deepseek_api.request_deepseek_json(
                "secret",
                "system",
                "user",
                attempts=2,
                retry_delays=(0,),
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(mocked_urlopen.call_count, 2)
        mocked_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
