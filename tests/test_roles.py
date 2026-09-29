"""Role specs choose each role's backend: claude-code, ollama:<model> or anthropic[:<model>]."""
import pytest

from big_brother.cloud import AnthropicConversation, AnthropicModel
from big_brother.ollama import DEFAULT_MODEL, OllamaClient
from big_brother.roles import RoleError, make_builder, make_writer, parse_role
from big_brother.writers import AgentWriter, ClaudeCodeWriter, OllamaConversation


def test_parse_role_reads_kind_and_model():
    assert parse_role("ollama:qwen2.5-coder:7b", "builder") == ("ollama", "qwen2.5-coder:7b")
    assert parse_role("anthropic", "builder") == ("anthropic", "claude-opus-5-5")
    assert parse_role("anthropic:claude-sonnet-5-5", "writer") == ("anthropic", "claude-sonnet-5-5")
    assert parse_role("claude-code", "writer") == ("claude-code", None)
    assert parse_role("claude-code:sonnet", "writer") == ("claude-code", "sonnet")
    assert parse_role("ollama", "builder") == ("ollama", DEFAULT_MODEL)


@pytest.mark.parametrize("spec,role", [("gpt:4", "writer"), ("", "builder"),
                                       ("claude-code", "builder"), ("ollama:", "writer")])
def test_bad_specs_are_refused(spec, role):
    with pytest.raises(RoleError):
        parse_role(spec, role)


def test_make_builder_picks_the_backend():
    assert isinstance(make_builder("ollama:m:1b"), OllamaClient)
    assert make_builder("ollama:m:1b").model == "m:1b"
    cloud = make_builder("anthropic:claude-sonnet-5-5", client=object())
    assert isinstance(cloud, AnthropicModel) and cloud.model == "claude-sonnet-5-5"


def test_make_writer_picks_the_backend():
    assert isinstance(make_writer("claude-code"), ClaudeCodeWriter)
    local = make_writer("ollama:m:7b", max_turns=9)
    assert isinstance(local, AgentWriter) and local.max_turns == 9
    chat = local.make_chat("sys", [])
    assert isinstance(chat, OllamaConversation) and chat.client.model == "m:7b"
    cloud = make_writer("anthropic", client=object())
    assert isinstance(cloud.make_chat("sys", []), AnthropicConversation)
