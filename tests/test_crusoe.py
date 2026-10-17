import contextlib
import http.server
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error

from agent_team import crusoe
from agent_team.__main__ import main
from agent_team.budget import Budget, estimate_prompt_tokens
from agent_team.calls import ModelOrder, call_with_fallback
from agent_team.crusoe import (CrusoeClient, DEFAULT_BASE_URL, price_per_1k, prices_for,
                               resolve_model)
from agent_team.models import ModelError, ModelReply

KEY = "sk-fake-crusoe-key-123"
MESSAGES = [{"role": "user", "content": "hello there"}]
NEMO = "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"


def ok_body(content="hi", usage=None, finish="stop"):
    body = {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    if usage is not None:
        body["usage"] = usage
    return 200, json.dumps(body).encode()


class FakeOpener:
    """Replays queued (status, body) or Exception items; records every request."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append((method, url, dict(headers), body, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client_with(*responses, **kw):
    opener = FakeOpener(*responses)
    return CrusoeClient(api_key=KEY, opener=opener, **kw), opener


def models_body(*ids):
    return 200, json.dumps({"object": "list", "data": [{"id": i} for i in ids]}).encode()


class CompleteTests(unittest.TestCase):
    def test_success_parses_text_tokens_and_request(self):
        client, opener = client_with(ok_body("answer", {"prompt_tokens": 5, "completion_tokens": 7,
                                                        "total_tokens": 12}))
        reply = client.complete("m/x", "worker", MESSAGES, 64)
        self.assertEqual(reply, ModelReply("answer", 12))
        self.assertEqual(len(opener.requests), 1)
        method, url, headers, body, _ = opener.requests[0]
        self.assertEqual((method, url), ("POST", f"{DEFAULT_BASE_URL}/chat/completions"))
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body), {"model": "m/x", "messages": MESSAGES, "max_tokens": 64})

    def test_tokens_fall_back_to_prompt_plus_completion_then_estimate(self):
        client, _ = client_with(ok_body(usage={"prompt_tokens": 5, "completion_tokens": 7}),
                                ok_body(usage={"total_tokens": -1}), ok_body(usage={"total_tokens": True}),
                                ok_body())
        self.assertEqual(client.complete("m", "r", MESSAGES, 64).tokens, 12)
        estimate = estimate_prompt_tokens(MESSAGES) + 64
        for _ in range(3):
            self.assertEqual(client.complete("m", "r", MESSAGES, 64).tokens, estimate)

    def test_tokens_use_larger_count_and_never_zero(self):
        client, _ = client_with(ok_body(usage={"prompt_tokens": 5, "completion_tokens": 7,
                                               "total_tokens": 3}),
                                ok_body(usage={"total_tokens": 0}),
                                ok_body(usage={"prompt_tokens": 0, "completion_tokens": 0,
                                               "total_tokens": 0}))
        self.assertEqual(client.complete("m", "r", MESSAGES, 64).tokens, 12)
        estimate = estimate_prompt_tokens(MESSAGES) + 64
        self.assertEqual(client.complete("m", "r", MESSAGES, 64).tokens, estimate)
        self.assertEqual(client.complete("m", "r", MESSAGES, 64).tokens, estimate)

    def test_deeply_nested_json_is_model_error(self):
        deep = b"[" * 200_000 + b"]" * 200_000
        for response in ((200, deep), (500, deep)):
            client, _ = client_with(response)
            with self.assertRaises(ModelError):
                client.complete("m", "r", MESSAGES, 8)

    def test_non_transport_exceptions_from_opener_are_model_errors(self):
        import http.client
        for exc in (http.client.IncompleteRead(b"x"), http.client.BadStatusLine("x"), ValueError("v")):
            client, _ = client_with(exc)
            with self.assertRaises(ModelError):
                client.complete("m", "r", MESSAGES, 8)

    def test_http_errors_mention_status(self):
        for status in (401, 404, 429, 500, 503):
            body = json.dumps({"error": {"message": "nope", "type": "t", "code": status}}).encode()
            client, opener = client_with((status, body))
            with self.assertRaises(ModelError) as ctx:
                client.complete("m", "r", MESSAGES, 8)
            self.assertIn(f"HTTP {status}", str(ctx.exception))
            self.assertIn("nope", str(ctx.exception))
            self.assertEqual(len(opener.requests), 1)

    def test_non_json_error_body(self):
        client, _ = client_with((503, b"<html>overloaded</html>"))
        with self.assertRaisesRegex(ModelError, "HTTP 503: <html>overloaded"):
            client.complete("m", "r", MESSAGES, 8)

    def test_malformed_shapes(self):
        for response in ((200, b"not json"), (200, b"{}"), (200, b'{"choices": []}'),
                         (200, b'{"choices": [{"message": {}}]}'), (200, b"[1]")):
            client, _ = client_with(response)
            with self.assertRaises(ModelError):
                client.complete("m", "r", MESSAGES, 8)

    def test_empty_content_mentions_finish_reason(self):
        for content in ("", "  \n", None):
            client, _ = client_with(ok_body(content, finish="length"))
            with self.assertRaisesRegex(ModelError, "length"):
                client.complete("m", "r", MESSAGES, 8)

    def test_opener_oserror_becomes_model_error(self):
        client, _ = client_with(socket.timeout("timed out"))
        with self.assertRaises(ModelError):
            client.complete("m", "r", MESSAGES, 8)

    def test_key_never_leaks_in_errors_or_repr(self):
        client, _ = client_with((401, json.dumps({"error": {"message": f"bad key {KEY}"}}).encode()),
                                (200, f"{KEY} not json".encode()),
                                ModelError(f"transport {KEY}"))
        for _ in range(3):
            with self.assertRaises(ModelError) as ctx:
                client.complete("m", "r", MESSAGES, 8)
            self.assertNotIn(KEY, str(ctx.exception))
        self.assertNotIn(KEY, repr(client))

    def test_error_message_truncated_and_redacted(self):
        client, _ = client_with((400, (KEY + "x" * 1000).encode()))
        with self.assertRaises(ModelError) as ctx:
            client.complete("m", "r", MESSAGES, 8)
        self.assertLessEqual(len(str(ctx.exception)), 300)
        self.assertIn("[redacted]", str(ctx.exception))


class ConstructorTests(unittest.TestCase):
    def test_rejects_bad_urls(self):
        for url in ("http://api.inference.crusoecloud.com/v1", "https://u:p@host/v1",
                    "https://host/v1?x=1", "https:///v1", "ftp://host"):
            with self.assertRaises(ValueError, msg=url):
                CrusoeClient(api_key=KEY, base_url=url, opener=FakeOpener())

    def test_rejects_bad_port_and_timeout(self):
        for url in ("https://host:abc/v1", "https://host:99999/v1", "https://[::1/v1"):
            with self.assertRaises(ValueError, msg=url):
                CrusoeClient(api_key=KEY, base_url=url, opener=FakeOpener())
        for timeout in (float("nan"), float("inf"), 0, -1, "5", True):
            with self.assertRaises(ValueError, msg=repr(timeout)):
                CrusoeClient(api_key=KEY, timeout=timeout, opener=FakeOpener())

    def test_requires_key(self):
        with self.assertRaisesRegex(ValueError, "CRUSOE_API_KEY"):
            CrusoeClient(api_key="", opener=FakeOpener())

    def test_from_env_reads_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(f"CRUSOE_API_KEY={KEY}\nCRUSOE_API_URL=https://example.test/v1\n")
            with mock.patch.dict("os.environ", {}, clear=True):
                client = CrusoeClient.from_env(path, opener=FakeOpener(ok_body()))
                client.complete("m", "r", MESSAGES, 8)
                self.assertEqual(client._opener.requests[0][1], "https://example.test/v1/chat/completions")
                path.write_text("CRUSOE_API_KEY=\n")
                with self.assertRaisesRegex(ValueError, "Add CRUSOE_API_KEY"):
                    CrusoeClient.from_env(path)
                path.write_text(f"CRUSOE_API_KEY={KEY}\n")
                self.assertEqual(CrusoeClient.from_env(path).base_url, DEFAULT_BASE_URL)


class DefaultOpenerTests(unittest.TestCase):
    def call(self, side_effect):
        opener = mock.Mock()
        opener.open.side_effect = side_effect
        with mock.patch.object(crusoe, "_build_opener", return_value=opener):
            return crusoe._urllib_opener("GET", "https://x.test/v1/models", {"A": "b"}, None, 5)

    def test_transport_errors_map_to_model_error(self):
        for exc in (urllib.error.URLError("dns"), socket.timeout("t"), TimeoutError("t"),
                    ConnectionResetError("r"), OSError("o")):
            with self.assertRaises(ModelError, msg=repr(exc)):
                self.call(exc)

    def test_http_error_returns_status_and_body(self):
        err = urllib.error.HTTPError("https://x.test", 429, "Too Many", {}, io.BytesIO(b"slow down"))
        self.assertEqual(self.call(err), (429, b"slow down"))

    def test_success_returns_status_and_body(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status, response.read1 = 200, io.BytesIO(b"{}").read1
        self.assertEqual(self.call(lambda *a, **k: response), (200, b"{}"))

    def test_default_opener_failure_through_client(self):
        client = CrusoeClient(api_key=KEY)
        opener = mock.Mock()
        opener.open.side_effect = socket.timeout("t")
        with mock.patch.object(crusoe, "_build_opener", return_value=opener):
            with self.assertRaises(ModelError):
                client.complete("m", "r", MESSAGES, 8)

    def test_redirect_status_maps_to_model_error(self):
        for call in (lambda c: c.complete("m", "r", MESSAGES, 8), lambda c: c.list_models()):
            client, opener = client_with((302, b"moved"))
            with self.assertRaisesRegex(ModelError, "HTTP 302: redirect refused"):
                call(client)
            self.assertEqual(len(opener.requests), 1)


class RedirectRefusalTests(unittest.TestCase):
    """Loopback only: a 302 must come back as a status and never be followed with the key."""

    @staticmethod
    def serve(handler):
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        return server, thread

    def test_302_is_not_followed(self):
        seen = []

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            log_message = lambda *a: None

        target, target_thread = self.serve(Target)
        location = f"http://127.0.0.1:{target.server_address[1]}/stolen"

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            log_message = lambda *a: None

        origin, origin_thread = self.serve(Redirector)
        try:
            url = f"http://127.0.0.1:{origin.server_address[1]}/v1/models"
            try:
                status, _ = crusoe._urllib_opener(
                    "GET", url, {"Authorization": f"Bearer {KEY}"}, None, 3)
                self.assertEqual(status, 302)
            except ModelError:
                pass
            self.assertEqual(seen, [])
        finally:
            for server, thread in ((origin, origin_thread), (target, target_thread)):
                server.shutdown()
                server.server_close()
                thread.join(5)


class RawServerTests(unittest.TestCase):
    """Loopback raw-socket servers driving the real _urllib_opener through CrusoeClient."""

    def serve(self, behavior):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(0.1)
        stop = threading.Event()

        def run():
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except OSError:
                    continue
                try:
                    conn.settimeout(2)
                    conn.recv(65536)
                    behavior(conn, stop)
                except OSError:
                    pass
                finally:
                    conn.close()
        thread = threading.Thread(target=run)
        thread.start()

        def cleanup():
            stop.set()
            thread.join(5)
            listener.close()
        self.addCleanup(cleanup)
        return listener.getsockname()[1]

    @staticmethod
    def client_for(port, timeout=5.0):
        local = f"http://127.0.0.1:{port}/v1"

        def opener(method, url, headers, body, tmo):  # test-only: https check stays on in production
            return crusoe._urllib_opener(method, local + url[len(DEFAULT_BASE_URL):], headers, body, tmo)
        return CrusoeClient(api_key=KEY, timeout=timeout, opener=opener)

    def test_truncated_body(self):
        def behavior(conn, stop):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n{\"choices\"")
        client = self.client_for(self.serve(behavior))
        with self.assertRaises(ModelError):
            client.complete("m", "r", MESSAGES, 8)

    def test_garbage_status_line(self):
        def behavior(conn, stop):
            conn.sendall(b"this is not http\r\n\r\n")
        client = self.client_for(self.serve(behavior))
        with self.assertRaises(ModelError):
            client.complete("m", "r", MESSAGES, 8)

    def test_truncated_error_body(self):
        def behavior(conn, stop):
            conn.sendall(b"HTTP/1.1 500 Oops\r\nContent-Length: 1000\r\n\r\npartial")
        client = self.client_for(self.serve(behavior))
        with self.assertRaises(ModelError):
            client.complete("m", "r", MESSAGES, 8)

    def test_trickling_server_hits_wall_clock_bound(self):
        def behavior(conn, stop):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            for _ in range(40):
                if stop.is_set():
                    return
                conn.sendall(b"x" * 5)
                time.sleep(0.3)
        client = self.client_for(self.serve(behavior), timeout=1)
        start = time.monotonic()
        with self.assertRaisesRegex(ModelError, "time/size limit"):
            client.complete("m", "r", MESSAGES, 8)
        self.assertLess(time.monotonic() - start, 2.5)

    def test_oversized_body_is_capped(self):
        def behavior(conn, stop):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 20000000\r\n\r\n")
            chunk = b"x" * 65536
            for _ in range(200):
                conn.sendall(chunk)
        with mock.patch.object(crusoe, "_MAX_BODY_BYTES", 256 * 1024):
            client = self.client_for(self.serve(behavior))
            with self.assertRaisesRegex(ModelError, "time/size limit"):
                client.complete("m", "r", MESSAGES, 8)


class ListModelsTests(unittest.TestCase):
    def test_sorted_unique_ids(self):
        client, opener = client_with(models_body("b/Two", "a/One", "a/One"))
        self.assertEqual(client.list_models(), ["a/One", "b/Two"])
        self.assertEqual(opener.requests[0][:2], ("GET", f"{DEFAULT_BASE_URL}/models"))
        self.assertIsNone(opener.requests[0][3])

    def test_errors(self):
        for response in ((401, b"{}"), (200, b"nope"), (200, b'{"data": 3}'), (200, b'{"data": [{}]}')):
            client, _ = client_with(response)
            with self.assertRaises(ModelError):
                client.list_models()


class ResolveTests(unittest.TestCase):
    AVAILABLE = ["zai-org/GLM-5.3", "zai-org/GLM-5.3-Flash", "openai/gpt-oss-120b", "deepseek/V4"]

    def test_exact_case_insensitive_beats_substring(self):
        self.assertEqual(resolve_model("ZAI-ORG/glm-5.3", self.AVAILABLE), "zai-org/GLM-5.3")
        self.assertEqual(resolve_model("glm-5.3", self.AVAILABLE), "zai-org/GLM-5.3")

    def test_unique_substring(self):
        self.assertEqual(resolve_model("gpt-oss", self.AVAILABLE), "openai/gpt-oss-120b")
        self.assertEqual(resolve_model("flash", self.AVAILABLE), "zai-org/GLM-5.3-Flash")

    def test_ambiguous_and_missing(self):
        with self.assertRaisesRegex(ValueError, "ambiguous.*GLM-5.3-Flash"):
            resolve_model("glm", self.AVAILABLE)
        with self.assertRaisesRegex(ValueError, "no model matches.*gpt-oss-120b"):
            resolve_model("llama", self.AVAILABLE)


class PriceTests(unittest.TestCase):
    def test_known_models_and_spellings(self):
        self.assertAlmostEqual(price_per_1k(NEMO), 0.0024)
        self.assertAlmostEqual(price_per_1k("nvidia/Nemotron-3-Super-120B-A12B"), 0.0024)
        self.assertAlmostEqual(price_per_1k("nvidia/Nemotron-3-Nano-30B"), 0.0002)
        self.assertAlmostEqual(price_per_1k("nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B"), 0.00183)
        self.assertAlmostEqual(price_per_1k("nvidia/NVIDIA-Nemotron-3.5-Super-VL"), 0.0004)
        self.assertAlmostEqual(price_per_1k("deepseek-ai/DeepSeek-V4-Flash"), 0.00028)
        self.assertAlmostEqual(price_per_1k("deepseek-ai/DeepSeek-V4.1-Flash"), 0.0012)
        self.assertAlmostEqual(price_per_1k("deepseek-ai/DeepSeek-V4-Pro"), 0.00348)
        self.assertAlmostEqual(price_per_1k("google/Gemma-4-31B"), 0.0004)
        self.assertAlmostEqual(price_per_1k("openai/gpt-oss-120b"), 0.00025)

    def test_glm_flash_precedence(self):
        self.assertAlmostEqual(price_per_1k("zai/GLM-5.3-Flash"), 0.0005)
        self.assertAlmostEqual(price_per_1k("zai-org/GLM-5.3"), 0.0044)

    def test_unknown_is_none_and_prices_for_raises(self):
        self.assertIsNone(price_per_1k("acme/mystery-1"))
        with self.assertRaisesRegex(ValueError, "acme/mystery-1"):
            prices_for([NEMO, "acme/mystery-1"])
        self.assertEqual(prices_for([NEMO]), {NEMO: price_per_1k(NEMO)})


class FallbackIntegrationTests(unittest.TestCase):
    def test_primary_429_twice_then_fallback_succeeds(self):
        fallback = "openai/gpt-oss-120b"
        rate = (429, b'{"error": {"message": "rate limited"}}')
        client, opener = client_with(rate, rate, ok_body("```done```", {"total_tokens": 40}))
        budget = Budget(prices=prices_for([NEMO, fallback]))
        parsed, attempts = call_with_fallback(
            ModelOrder(NEMO, fallback), client, budget, role="worker", messages=MESSAGES,
            parse=lambda text: text, max_tokens=64)
        self.assertEqual(parsed, "```done```")
        self.assertEqual([a["outcome"] for a in attempts], ["model_error", "model_error", "ok"])
        self.assertEqual([json.loads(r[3])["model"] for r in opener.requests], [NEMO, NEMO, fallback])
        self.assertTrue(budget.snapshot()["priced"])
        self.assertEqual(budget.snapshot()["tokens_used"], 40)


class ModelsCliTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_lists_models_and_resolves(self):
        client, _ = client_with(models_body(NEMO, "acme/mystery-1"))
        with mock.patch.object(CrusoeClient, "from_env", return_value=client):
            code, out, err = self.run_cli(["models", "--resolve", "nemotron-3-super",
                                           "--resolve", "nope"])
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(data["base_url"], DEFAULT_BASE_URL)
        self.assertEqual(data["models"], [
            {"id": "acme/mystery-1", "usd_per_1k_conservative": None},
            {"id": NEMO, "usd_per_1k_conservative": price_per_1k(NEMO)}])
        self.assertEqual(data["resolved"]["nemotron-3-super"], NEMO)
        self.assertTrue(data["resolved"]["nope"].startswith("error:"))
        self.assertNotIn(KEY, out)

    def test_missing_key_exits_1(self):
        with mock.patch.object(crusoe, "read_environment", return_value={}):
            code, out, err = self.run_cli(["models"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("CRUSOE_API_KEY", json.loads(err)["error"])

    def test_model_error_exits_1_without_key(self):
        client, _ = client_with((401, json.dumps({"error": {"message": f"bad {KEY}"}}).encode()))
        with mock.patch.object(CrusoeClient, "from_env", return_value=client):
            code, out, err = self.run_cli(["models"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("HTTP 401", err)
        self.assertNotIn(KEY, err)


if __name__ == "__main__":
    unittest.main()
