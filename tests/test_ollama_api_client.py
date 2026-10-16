"""Regression tests for the Ollama HTTP client (servercloud.modules.ollama_api).

Guards against the closed-socket bug: _request() used to return the response
from inside a `with urlopen(...)` block, so the socket was already closed by
the time callers read it — every endpoint looked dead and the app silently
fell back to demo mode even with a live Ollama running.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from servercloud.modules import ollama_api as oa


class _FakeOllama(BaseHTTPRequestHandler):
    """Minimal stand-in for the Ollama REST API (GET/POST handled alike)."""

    def _respond(self):
        if self.path == "/api/tags":
            body = json.dumps({"models": [{"name": "llama3:latest"},
                                          {"name": "mistral"}]}).encode()
        elif self.path == "/api/version":
            body = b'{"version":"0.9.9"}'
        elif self.path == "/api/pull":
            body = (b'{"status":"downloading 1234abc"}\n'
                    b'{"status":"success","digest":"sha256:x"}\n')
        else:  # /api/generate — streamed NDJSON
            body = (b'{"response":"hel","done":false}\n'
                    b'{"response":"lo","done":true,"eval_count":2,'
                    b'"eval_duration":1000000,"prompt_eval_count":3}\n')
        length = len(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *args):  # silence test output
        pass


class OllamaClientTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _FakeOllama)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls._old_url = oa.os.environ.get("SERVERCLOUD_OLLAMA_URL")
        oa.os.environ["SERVERCLOUD_OLLAMA_URL"] = \
            "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        if cls._old_url is None:
            oa.os.environ.pop("SERVERCLOUD_OLLAMA_URL", None)
        else:
            oa.os.environ["SERVERCLOUD_OLLAMA_URL"] = cls._old_url

    # --- liveness -----------------------------------------------------
    def test_is_alive_true_against_live_server(self):
        """The core regression: live server must NOT look dead."""
        self.assertTrue(oa.is_alive())

    def test_server_version(self):
        self.assertEqual(oa.server_version(), "0.9.9")

    def test_list_local_models(self):
        self.assertEqual(oa.list_local_models(),
                         ["llama3:latest", "mistral"])

    # --- streaming generate -------------------------------------------
    def test_generate_stream_reads_body(self):
        text, n_tok, elapsed, stats = oa.generate_stream(
            "llama3:latest", "hi", tokens_per_sec=10000.0)
        self.assertEqual(text, "hello")
        self.assertEqual(n_tok, 2)
        self.assertGreaterEqual(elapsed, 0.0)
        self.assertEqual(stats["source"], "ollama")
        self.assertEqual(stats["eval_count"], 2)

    # --- pull ----------------------------------------------------------
    def test_pull_model_success(self):
        seen = []
        ok = oa.pull_model("test:model", on_progress=seen.append)
        self.assertTrue(ok)
        self.assertTrue(any("success" in str(s.get("status", ""))
                            for s in seen))

    # --- negative path: nothing listening -----------------------------
    def test_is_alive_false_when_server_down(self):
        dead_port = self.port + 1  # nothing bound here
        old = oa.os.environ["SERVERCLOUD_OLLAMA_URL"]
        try:
            oa.os.environ["SERVERCLOUD_OLLAMA_URL"] = \
                "http://127.0.0.1:%d" % dead_port
            self.assertFalse(oa.is_alive())
            self.assertEqual(oa.server_version(), "?")
            self.assertEqual(oa.list_local_models(), [])
        finally:
            oa.os.environ["SERVERCLOUD_OLLAMA_URL"] = old


if __name__ == "__main__":
    unittest.main()
