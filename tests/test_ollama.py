"""The Ollama client talks to the local HTTP API; the server runs only on demand."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from big_brother.ollama import OllamaClient, OllamaError, ollama_on_demand


class FakeOllama:
    """A tiny HTTP server that records requests and answers like Ollama."""

    def __init__(self, status=200, reply="hello"):
        self.requests: list[tuple[str, str, dict | None]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                self._answer(200, {"version": "0.0.0"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(("POST", self.path, body))
                if status == 200:
                    self._answer(200, {"message": {"role": "assistant", "content": reply}})
                else:
                    self._answer(status, {"error": f"model '{body['model']}' not found"})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    server = FakeOllama(reply="FILE: src/a.py")
    yield server
    server.close()


def test_chat_posts_model_messages_and_context_size(fake):
    client = OllamaClient(host=fake.host, model="m:1b", num_ctx=4321)
    msgs = [{"role": "user", "content": "hi"}]
    assert client.chat(msgs) == "FILE: src/a.py"
    method, path, body = fake.requests[-1]
    assert (method, path) == ("POST", "/api/chat")
    assert body["model"] == "m:1b"
    assert body["messages"] == msgs
    assert body["stream"] is False
    assert body["options"]["num_ctx"] == 4321


def test_is_up_true_when_server_answers(fake):
    assert OllamaClient(host=fake.host).is_up()


def test_is_up_false_when_nothing_listens():
    assert not OllamaClient(host="http://127.0.0.1:9", timeout=1).is_up()


def test_missing_model_names_the_pull_script():
    server = FakeOllama(status=404)
    try:
        with pytest.raises(OllamaError, match="scripts/pull_model.sh"):
            OllamaClient(host=server.host, model="nope:1b").chat([])
    finally:
        server.close()


def test_other_http_error_is_ollama_error():
    server = FakeOllama(status=500)
    try:
        with pytest.raises(OllamaError, match="500"):
            OllamaClient(host=server.host).chat([])
    finally:
        server.close()


def test_unreachable_server_is_ollama_error():
    with pytest.raises(OllamaError, match="not reachable"):
        OllamaClient(host="http://127.0.0.1:9", timeout=1).chat([])


# on demand

class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.waited = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True


def test_on_demand_leaves_a_running_server_alone():
    started = []
    with ollama_on_demand(is_up=lambda: True, start=lambda: started.append(1)):
        pass
    assert started == []


def test_on_demand_starts_waits_and_stops_a_server_it_started():
    proc = FakeProcess()
    ups = iter([False, False, True])
    with ollama_on_demand(is_up=lambda: next(ups), start=lambda: proc, poll=0):
        assert not proc.terminated
    assert proc.terminated and proc.waited


def test_on_demand_stops_the_server_when_the_block_raises():
    proc = FakeProcess()
    ups = iter([False, True])
    with pytest.raises(RuntimeError):
        with ollama_on_demand(is_up=lambda: next(ups), start=lambda: proc, poll=0):
            raise RuntimeError("boom")
    assert proc.terminated


def test_on_demand_gives_up_if_the_server_never_answers():
    proc = FakeProcess()
    with pytest.raises(OllamaError, match="did not start"):
        with ollama_on_demand(is_up=lambda: False, start=lambda: proc, wait=0.05, poll=0.01):
            pass
    assert proc.terminated
