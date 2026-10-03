"""Lightweight, persisted Ollama capability profiles."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .ollama import OllamaError
from .preferences import preferences_path


PING_TOOL = [{"type": "function", "function": {"name": "ping",
              "description": "Return a capability-test ping.",
              "parameters": {"type": "object", "properties": {},
                             "required": [], "additionalProperties": False}}}]


class ModelProfiles:
    def __init__(self, path=None):
        self.path = Path(path or (preferences_path().parent / "model-profiles.json"))
        self.data = self._load()

    def _load(self):
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def get(self, model):
        return self.data.get(model)

    def all(self):
        return dict(self.data)

    @staticmethod
    def _tokens_per_second(response):
        count, duration = response.get("eval_count", 0), response.get("eval_duration", 0)
        return round(count / (duration / 1_000_000_000), 2) if count and duration else None

    def probe(self, client, model, context_tokens=8192, emit=lambda _: None, force=False):
        if not force and model in self.data:
            return self.data[model]
        emit(f"Profiling {model}: testing thinking, native tools, JSON, and speed...")
        options = {"temperature": 0, "num_ctx": max(4096, min(int(context_tokens or 8192), 8192)),
                   "num_predict": 96}
        native_successes, latencies, speeds = 0, [], []
        thinking_supported = True
        errors = []
        for trial in range(2):
            payload = {"model": model, "messages": [{"role": "user", "content":
                       "Call the ping tool once with no arguments."}], "stream": False,
                       "tools": PING_TOOL, "options": options, "keep_alive": "5m"}
            if trial == 0 and thinking_supported:
                payload["think"] = "low"
            started = time.monotonic()
            try:
                response = client.request("/api/chat", payload)
            except OllamaError as exc:
                if "does not support thinking" in str(exc).lower() and "think" in payload:
                    thinking_supported = False
                    payload.pop("think", None)
                    try:
                        response = client.request("/api/chat", payload)
                    except OllamaError as retry_exc:
                        errors.append(str(retry_exc))
                        continue
                else:
                    errors.append(str(exc))
                    continue
            latencies.append(time.monotonic() - started)
            speeds.append(self._tokens_per_second(response))
            if response.get("message", {}).get("tool_calls"):
                native_successes += 1
        json_supported = False
        started = time.monotonic()
        try:
            response = client.request("/api/chat", {"model": model, "messages": [{"role": "user",
                "content": 'Return exactly this JSON object: {"ok":true}'}], "stream": False,
                "format": "json", "options": options, "keep_alive": "5m"})
            json.loads(response.get("message", {}).get("content", ""))
            json_supported = True
            latencies.append(time.monotonic() - started)
            speeds.append(self._tokens_per_second(response))
        except (OllamaError, ValueError, TypeError) as exc:
            errors.append(str(exc))
        reliability = native_successes / 2
        max_latency = max(latencies) if latencies else 30
        profile = {
            "model": model, "tested_at": datetime.now(timezone.utc).isoformat(),
            "native_tools_supported": native_successes > 0,
            "native_tool_reliability": reliability,
            "json_supported": json_supported, "thinking_supported": thinking_supported,
            "stable_context_tokens": options["num_ctx"],
            "recommended_context_tokens": max(4096, min(int(context_tokens or 8192), 32768)),
            "recommended_timeout_seconds": max(120, min(3600, round(max_latency * 8))),
            "tokens_per_second": round(sum(v for v in speeds if v) / len([v for v in speeds if v]), 2)
                                 if any(speeds) else None,
            "recommended_tool_mode": "native" if reliability >= 0.5 else ("json" if json_supported else "native"),
            "probe_errors": errors[-3:],
        }
        self.data[model] = profile
        self._save()
        emit(f"Profile saved: native reliability {reliability:.0%}, JSON {'yes' if json_supported else 'no'}.")
        return profile
