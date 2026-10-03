"""Local Ollama HTTP transport; no external package required."""
import json
import http.client
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request


class OllamaError(RuntimeError):
    pass


class OllamaCancelled(OllamaError):
    pass


class OllamaClient:
    def __init__(self, base_url, timeout=600, trace=None):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("base_url must be an http(s) Ollama server URL without credentials")
        self.base_url, self.timeout = base_url.rstrip("/"), timeout
        self.trace = trace or (lambda event, data: None)
        self.cancelled = threading.Event()
        # Ignore corporate HTTP proxies for local/offline inference.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, endpoint, payload=None):
        # On Windows, a blocking socket read can postpone KeyboardInterrupt.
        # Only the daemon worker blocks on the network; the main thread checks
        # Python signals every 100 ms. Exiting the CLI closes its sockets too.
        completed = queue.Queue(maxsize=1)
        def send():
            try:
                completed.put((True, self._request_blocking(endpoint, payload)))
            except BaseException as exc:
                completed.put((False, exc))
        worker = threading.Thread(target=send, daemon=True, name="ollama-request")
        try:
            worker.start()
        except RuntimeError as exc:
            # On Windows a Ctrl+C delivered during Thread.start's tiny internal
            # condition wait can surface as "release unlocked lock" instead of
            # the original KeyboardInterrupt. Preserve the user's stop intent.
            if "unlocked lock" in str(exc):
                raise KeyboardInterrupt from exc
            raise
        while True:
            if self.cancelled.is_set():
                raise OllamaCancelled("Paused by user; the Ollama reply was ignored")
            try:
                success, result = completed.get(timeout=0.1)
            except queue.Empty:
                continue
            if not success:
                raise result
            return result

    def cancel_current(self):
        self.cancelled.set()

    def reset_cancel(self):
        self.cancelled.clear()

    def _request_blocking(self, endpoint, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + endpoint, data=data,
                                         headers={"Content-Type": "application/json"})
        self.trace("request", {"method": request.get_method(), "url": request.full_url,
                               "payload": payload})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self.trace("error", {"status": exc.code, "body": detail})
            raise OllamaError(f"Ollama HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as exc:
            self.trace("error", {"error": str(exc)})
            if "timed out" in str(exc).lower():
                raise OllamaError(f"Ollama did not respond within {self.timeout} seconds. "
                                  "The model may still be computing or may be memory constrained. "
                                  "Increase timeout_seconds, reduce num_ctx, or inspect Ollama's server log.") from exc
            raise OllamaError(f"Cannot reach Ollama at {self.base_url}: {exc}. "
                              "Start ollama serve and check the configured model/timeout.") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.trace("error", {"error": str(exc)})
            raise OllamaError("Ollama returned invalid JSON") from exc
        self.trace("response", result)
        if not isinstance(result, dict):
            raise OllamaError("Ollama response must be a JSON object")
        if result.get("error"):
            raise OllamaError(str(result["error"]))
        return result

    def models(self):
        return [m["name"] for m in self.request("/api/tags").get("models", [])]

    def chat(self, messages, schemas, config, mode):
        payload = {"model": config.model, "messages": messages, "stream": False,
                   "options": config.options, "keep_alive": "5m"}
        if config.think is not None:
            payload["think"] = config.think
        if mode == "native":
            payload["tools"] = schemas
        else:
            # Constrain syntax as well as requesting JSON in the prompt. Final
            # answers use an {"answer": "..."} envelope in this transport mode.
            payload["format"] = "json"
        response = self.request("/api/chat", payload)
        if not isinstance(response.get("message"), dict):
            raise OllamaError("Ollama response has no assistant message")
        return response
