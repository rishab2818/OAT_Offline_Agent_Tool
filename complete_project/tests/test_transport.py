import json
import threading
import unittest
import signal
import time
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from local_agent.config import Config
from local_agent.ollama import OllamaClient, OllamaError, OllamaCancelled


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload)
                if payload["model"] == "missing":
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error":"model not found"}')
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"message":{"role":"assistant","content":"ok"},"done":true}')
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = OllamaClient(f"http://127.0.0.1:{self.server.server_port}", timeout=5)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_real_http_payload_native_and_text_modes(self):
        messages = [{"role": "user", "content": "hello"}]
        config = Config(think="low")
        tools = [{"type": "function", "function": {"name": "example"}}]
        self.assertEqual(self.client.chat(messages, tools, config, "native")["message"]["content"], "ok")
        self.assertEqual(self.requests[0]["tools"], tools)
        self.assertEqual(self.requests[0]["think"], "low")
        config.think = None
        self.client.chat(messages, tools, config, "json")
        self.assertNotIn("tools", self.requests[1])
        self.assertEqual(self.requests[1]["format"], "json")
        self.assertNotIn("think", self.requests[1])

    def test_server_error_is_actionable(self):
        with self.assertRaisesRegex(OllamaError, "model not found"):
            self.client.chat([], [], Config(model="missing"), "native")

    def test_ctrl_c_interrupts_blocked_network_request(self):
        blocked = threading.Event()
        entered = threading.Event()
        def stalled(*args):
            entered.set()
            blocked.wait(5)
            return {}
        def interrupt():
            entered.wait(2)
            signal.raise_signal(signal.SIGINT)
        interrupter = threading.Thread(target=interrupt, daemon=True)
        start = time.monotonic()
        try:
            with patch.object(self.client, "_request_blocking", side_effect=stalled):
                interrupter.start()
                with self.assertRaises(KeyboardInterrupt):
                    self.client.request("/api/chat", {})
            self.assertLess(time.monotonic() - start, 2)
        finally:
            blocked.set()
            interrupter.join(timeout=2)

    def test_trace_contains_actual_request_and_response(self):
        events = []
        self.client.trace = lambda event, data: events.append((event, data))
        messages = [{"role": "user", "content": "Read README.md"},
                    {"role": "tool", "tool_name": "read_file", "content": '{"ok":false,"error":"File not found"}'}]
        self.client.chat(messages, [], Config(), "native")
        self.assertEqual(events[0][0], "request")
        self.assertEqual(events[0][1]["payload"]["messages"], messages)
        self.assertEqual(events[-1][0], "response")
        self.assertEqual(events[-1][1]["message"]["content"], "ok")

    def test_programmatic_stop_interrupts_blocked_network_wait(self):
        blocked = threading.Event()
        entered = threading.Event()
        def stalled(*args):
            entered.set()
            blocked.wait(5)
            return {}
        def stop():
            entered.wait(2)
            self.client.cancel_current()
        stopper = threading.Thread(target=stop, daemon=True)
        start = time.monotonic()
        try:
            with patch.object(self.client, "_request_blocking", side_effect=stalled):
                stopper.start()
                with self.assertRaises(OllamaCancelled):
                    self.client.request("/api/chat", {})
            self.assertLess(time.monotonic() - start, 2)
        finally:
            blocked.set()
            stopper.join(timeout=2)
