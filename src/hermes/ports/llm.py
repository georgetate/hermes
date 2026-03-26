from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class Message:
    """One message in the model conversation history."""

    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None


@dataclass(slots=True)
class Tool:
    """LLM-visible tool definition using an OpenAI-style function schema."""

    name: str
    description: str
    input_schema: dict[str, object]
    tool_type: str = "function"
    requires_confirmation: bool = False


@dataclass(slots=True)
class ToolCall:
    """A structured tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, object]


@dataclass(slots=True)
class Usage:
    """Token usage metadata returned by the model provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class LLMResponse:
    """Normalized result of one model generation step."""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None


class LLM(ABC):
    """
    Model I/O contract for Hermes.

    Implementations receive the conversation so far plus any available tools,
    then return either assistant text, tool calls, or both.
    """

    @abstractmethod
    def generate(
        self,
        messages: list[Message],
        *,
        tools: list[Tool] | None = None,
    ) -> LLMResponse:
        """Generate the next assistant step from conversation history."""

        raise NotImplementedError
