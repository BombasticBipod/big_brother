"""Talk to the local Ollama server, and run it only for the length of a build.

`OllamaClient.chat` posts to `/api/chat` and returns the reply text. Given
`on_token`, it streams: each chunk goes to `on_token` as it arrives, so a
window can show the model writing. `chat_message` sends tool definitions and
returns the whole reply message, tool calls included, for a model that plays
the test writer. The context size is always sent: a server default that is too
small silently drops the front of a long prompt, where the format rules are.

`ollama_on_demand` starts `ollama serve` only if nothing answers, waits for it,
and stops it at the end of the block. A server that was already running is
left alone. Ollama is never left running as a service.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from typing import Iterator

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5-coder:3b"
DEFAULT_NUM_CTX = 8192


class OllamaError(Exception):
    """The Ollama server could not answer the request."""


class OllamaClient:
    def __init__(self, host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL,
                 num_ctx: int = DEFAULT_NUM_CTX, timeout: float = 600, temperature: float = 0.2):
        self.host = host.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.temperature = temperature

    def is_up(self) -> bool:
        try:
            with urllib.request.urlopen(self.host + "/api/version", timeout=min(self.timeout, 2)):
                return True
        except (urllib.error.URLError, OSError):
            return False

    def chat(self, messages: list[dict],
             on_token: Callable[[str], None] | None = None) -> str:
        if on_token is None:
            return self._post(messages, stream=False)["message"]["content"]
        return self._post(messages, stream=True, on_token=on_token)

    def chat_message(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        return self._post(messages, stream=False, tools=tools)["message"]

    def _post(self, messages: list[dict], stream: bool, tools: list[dict] | None = None,
              on_token: Callable[[str], None] | None = None):
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "options": {"num_ctx": self.num_ctx, "temperature": self.temperature},
        }
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(self.host + "/api/chat", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if not stream:
                    return json.loads(response.read())
                parts = []
                for line in response:
                    if not line.strip():
                        continue
                    piece = json.loads(line).get("message", {}).get("content", "")
                    if piece:
                        parts.append(piece)
                        on_token(piece)
                return "".join(parts)
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise OllamaError(f"model {self.model} not found; pull it with "
                                  f"scripts/pull_model.sh {self.model}") from None
            raise OllamaError(f"Ollama answered HTTP {err.code}") from None
        except (urllib.error.URLError, OSError) as err:
            raise OllamaError(f"Ollama not reachable at {self.host}: {err}") from None


def _start_server() -> subprocess.Popen:
    return subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


@contextmanager
def ollama_on_demand(is_up: Callable[[], bool] | None = None,
                     start: Callable[[], subprocess.Popen] = _start_server,
                     wait: float = 30, poll: float = 0.5) -> Iterator[None]:
    """Make sure Ollama is up for the block; stop it afterwards only if this started it."""
    is_up = is_up or OllamaClient().is_up
    if is_up():
        yield
        return
    proc = start()
    try:
        deadline = time.monotonic() + wait
        while not is_up():
            if time.monotonic() >= deadline:
                raise OllamaError(f"Ollama did not start within {wait}s")
            time.sleep(poll)
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=10)
