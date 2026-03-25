import json
from collections.abc import Callable
from dataclasses import dataclass, field

from hermes.ports.llm import LLM, LLMResponse, Message, Tool, ToolCall


ToolHandler = Callable[[dict[str, object]], object]


@dataclass(slots=True)
class ConversationService:
    llm: LLM
    tools: list[Tool] = field(default_factory=list)
    tool_handlers: dict[str, ToolHandler] = field(default_factory=dict)
    history: list[Message] = field(default_factory=list)
    context_messages: list[Message] = field(default_factory=list)
    system_prompt: str | None = None
    max_tool_rounds: int = 8
    max_recent_messages: int = 8
    max_summary_chars: int = 1800
    max_message_chars: int = 1200
    max_tool_message_chars: int = 1800
    max_tool_description_chars: int = 120
    max_tool_property_description_chars: int = 80

    def __post_init__(self) -> None:
        self._rebuild_context_messages()

    def register_tool(self, tool: Tool, handler: ToolHandler) -> None:
        self.tools.append(self._compact_tool(tool))
        self.tool_handlers[tool.name] = handler
    
    def handle_user_input(self, user_text: str) -> str:
        self.history.append(Message(role="user", content=user_text))

        for _ in range(self.max_tool_rounds):
            self._rebuild_context_messages()
            response = self.llm.generate(
                self.context_messages,
                tools=self.tools or None,
            )

            final_response = self._record_llm_response(response)
            if not response.tool_calls:
                return final_response

            for tool_call in response.tool_calls:
                tool_output = self._execute_tool_call(tool_call)
                self.history.append(
                    Message(
                        role="tool",
                        content=tool_output,
                        name=tool_call.name,
                        tool_call_id=tool_call.id,
                    )
                )

        raise RuntimeError(
            "Conversation exceeded the maximum number of tool rounds."
        )

    def _record_llm_response(self, response: LLMResponse) -> str:
        assistant_text = response.content or ""
        if assistant_text:
            self.history.append(
                Message(role="assistant", content=assistant_text)
            )
        return assistant_text

    def _execute_tool_call(self, tool_call: ToolCall) -> str:
        handler = self.tool_handlers.get(tool_call.name)
        if handler is None:
            return f"Tool '{tool_call.name}' is not registered."

        try:
            result = handler(tool_call.arguments)
        except Exception as exc:
            return f"Tool '{tool_call.name}' failed: {exc}"

        return self._serialize_tool_result(result)

    @staticmethod
    def _serialize_tool_result(result: object) -> str:
        if isinstance(result, str):
            return result

        try:
            return json.dumps(result, default=str)
        except TypeError:
            return str(result)

    def _rebuild_context_messages(self) -> None:
        context_messages: list[Message] = []

        if self.system_prompt:
            context_messages.append(
                Message(role="system", content=self.system_prompt)
            )

        summary = self._build_history_summary()
        if summary:
            context_messages.append(
                Message(
                    role="system",
                    content=(
                        "Conversation summary from earlier turns:\n"
                        f"{summary}"
                    ),
                )
            )

        recent_messages = self.history[-self.max_recent_messages :]
        for message in recent_messages:
            context_messages.append(self._trim_message_for_context(message))

        self.context_messages = context_messages

    def _build_history_summary(self) -> str | None:
        if len(self.history) <= self.max_recent_messages:
            return None

        older_messages = self.history[:-self.max_recent_messages]
        summary_lines = [
            self._summarize_message(message)
            for message in older_messages
        ]
        summary = "\n".join(
            line for line in summary_lines if line
        ).strip()
        if not summary:
            return None
        return self._trim_text(summary, self.max_summary_chars)

    def _summarize_message(self, message: Message) -> str:
        role_label = message.role
        if message.role == "tool" and message.name:
            role_label = f"tool:{message.name}"

        limit = (
            self.max_tool_message_chars
            if message.role == "tool"
            else self.max_message_chars
        )
        snippet = self._trim_text(message.content, limit // 3)
        if not snippet:
            return ""
        return f"{role_label}: {snippet}"

    def _trim_message_for_context(self, message: Message) -> Message:
        limit = (
            self.max_tool_message_chars
            if message.role == "tool"
            else self.max_message_chars
        )
        return Message(
            role=message.role,
            content=self._trim_text(message.content, limit),
            name=message.name,
            tool_call_id=message.tool_call_id,
        )

    def _compact_tool(self, tool: Tool) -> Tool:
        compact_schema = dict(tool.input_schema)
        properties = compact_schema.get("properties")
        if isinstance(properties, dict):
            compact_properties: dict[str, object] = {}
            for key, value in properties.items():
                if isinstance(value, dict):
                    compact_value = dict(value)
                    description = compact_value.get("description")
                    if isinstance(description, str):
                        compact_value["description"] = self._trim_text(
                            description,
                            self.max_tool_property_description_chars,
                        )
                    compact_properties[key] = compact_value
                else:
                    compact_properties[key] = value
            compact_schema["properties"] = compact_properties

        return Tool(
            name=tool.name,
            description=self._trim_text(
                tool.description,
                self.max_tool_description_chars,
            ),
            input_schema=compact_schema,
            tool_type=tool.tool_type,
        )

    @staticmethod
    def _trim_text(text: str, max_chars: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= max_chars:
            return normalized
        if max_chars <= 3:
            return normalized[:max_chars]
        return normalized[: max_chars - 3].rstrip() + "..."
