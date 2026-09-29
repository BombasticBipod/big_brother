"""Cloud models through the Anthropic SDK, with a fake client: no network in the default suite."""
from types import SimpleNamespace as NS

import pytest

from big_brother.cloud import DEFAULT_CLOUD_MODEL, AnthropicConversation, AnthropicModel, CloudError


class FakeStream:
    def __init__(self, texts, final):
        self.text_stream = iter(texts)
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


class FakeClient:
    """Records every request; answers from a script of (text chunks, final message)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[dict] = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kw):
        self.requests.append(kw)
        texts, final = self.answers.pop(0)
        return FakeStream(texts, final)


def text_block(text):
    return NS(type="text", text=text)


def tool_block(id, name, args):
    return NS(type="tool_use", id=id, name=name, input=args)


def final(*blocks, stop="end_turn"):
    return NS(content=list(blocks), stop_reason=stop)


def test_builder_model_streams_tokens_and_returns_the_text():
    client = FakeClient((["FILE: ", "src/a.py"], final(text_block("FILE: src/a.py"))))
    tokens = []
    model = AnthropicModel(model="claude-sonnet-5-5", client=client)
    text = model.chat([{"role": "system", "content": "rules"}, {"role": "user", "content": "go"}],
                      on_token=tokens.append)
    assert text == "FILE: src/a.py"
    assert tokens == ["FILE: ", "src/a.py"]
    request = client.requests[-1]
    assert request["model"] == "claude-sonnet-5-5"
    assert request["system"] == "rules"
    assert request["messages"] == [{"role": "user", "content": "go"}]
    assert request["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in request["betas"]


def test_default_cloud_model_is_opus():
    assert DEFAULT_CLOUD_MODEL == "claude-opus-5-5"


def test_refusal_is_a_cloud_error():
    client = FakeClient(([], final(stop="refusal")))
    with pytest.raises(CloudError, match="refus"):
        AnthropicModel(client=client).chat([{"role": "user", "content": "go"}])


TOOLS = [{"name": "next_requirement", "description": "next item",
          "inputSchema": {"type": "object", "properties": {}}}]


def test_conversation_sends_tools_returns_calls_and_feeds_results_back():
    call = tool_block("t1", "next_requirement", {})
    client = FakeClient((["looking"], final(text_block("looking"), call, stop="tool_use")),
                        (["done"], final(text_block("done"))))
    chat = AnthropicConversation(system="sys", tools=TOOLS, client=client)
    shown = []
    turn = chat.send("goal: add", on_text=shown.append)
    assert turn.text == "looking"
    assert [(c.id, c.name, c.args) for c in turn.calls] == [("t1", "next_requirement", {})]
    assert client.requests[0]["tools"] == [{"name": "next_requirement", "description": "next item",
                                            "input_schema": {"type": "object", "properties": {}}}]
    turn = chat.results([(turn.calls[0], "1: add (open)", False)], on_text=shown.append)
    assert turn.calls == [] and turn.text == "done"
    assert shown == ["looking", "done"]
    second = client.requests[1]["messages"]
    assert second[1]["content"][1] is call           # assistant turn appended whole
    assert second[2]["content"] == [{"type": "tool_result", "tool_use_id": "t1",
                                     "content": "1: add (open)", "is_error": False}]
