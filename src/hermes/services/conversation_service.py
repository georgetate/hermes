import json
from collections.abc import Callable
from dataclasses import dataclass, field

from hermes.ports.llm import LLM, LLMResponse, Message, Tool, ToolCall


ToolHandler = Callable[[dict[str, object]], object]
ConfirmationFormatter = Callable[[dict[str, object]], str]


@dataclass(slots=True)
class PendingToolAction:
    """A destructive tool call waiting for explicit user confirmation."""

    tool_call: ToolCall
    confirmation_preview: str


@dataclass(slots=True)
class ConversationService:
    llm: LLM
    tools: list[Tool] = field(default_factory=list)
    tool_handlers: dict[str, ToolHandler] = field(default_factory=dict)
    confirmation_formatters: dict[str, ConfirmationFormatter] = field(default_factory=dict)
    tools_requiring_confirmation: set[str] = field(default_factory=set)
    history: list[Message] = field(default_factory=list)
    context_messages: list[Message] = field(default_factory=list)
    pending_action: PendingToolAction | None = None
    system_prompt: str | None = None
    max_tool_rounds: int = 8
    max_recent_messages: int = 8
    max_summary_chars: int = 1800
    max_message_chars: int = 1200
    max_tool_message_chars: int = 3600
    max_tool_description_chars: int = 120
    max_tool_property_description_chars: int = 80

    def __post_init__(self) -> None:
        self._rebuild_context_messages()

    def register_tool(
        self,
        tool: Tool,
        handler: ToolHandler,
        *,
        confirmation_formatter: ConfirmationFormatter | None = None,
    ) -> None:
        self.tools.append(self._compact_tool(tool))
        self.tool_handlers[tool.name] = handler
        if tool.requires_confirmation:
            self.tools_requiring_confirmation.add(tool.name)
        if confirmation_formatter is not None:
            self.confirmation_formatters[tool.name] = confirmation_formatter
    
    def handle_user_input(self, user_text: str) -> str:
        self.history.append(Message(role="user", content=user_text))
        if self.pending_action is not None:
            return self._handle_pending_action_response(user_text)

        return self._run_llm_turns(tool_result_cache={})

    def _run_llm_turns(self, *, tool_result_cache: dict[str, str]) -> str:
        """Run the assistant/tool loop until a user-facing response is ready."""

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
                if self._requires_confirmation(tool_call):
                    confirmation_message = self._queue_pending_action(tool_call)
                    self.history.append(
                        Message(role="assistant", content=confirmation_message)
                    )
                    return confirmation_message

                cache_key = self._side_effecting_tool_cache_key(tool_call)
                if cache_key is not None and cache_key in tool_result_cache:
                    tool_output = tool_result_cache[cache_key]
                else:
                    tool_output = self._execute_tool_call(tool_call)
                    if cache_key is not None:
                        tool_result_cache[cache_key] = tool_output
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

    def _handle_pending_action_response(self, user_text: str) -> str:
        """Resolve a pending destructive action via explicit confirm/cancel."""

        pending_action = self.pending_action
        if pending_action is None:
            return self._run_llm_turns(tool_result_cache={})

        if self._is_confirmation_reply(user_text):
            self.pending_action = None
            tool_output = self._execute_tool_call(pending_action.tool_call)
            self.history.append(
                Message(
                    role="tool",
                    content=tool_output,
                    name=pending_action.tool_call.name,
                    tool_call_id=pending_action.tool_call.id,
                )
            )
            return self._run_llm_turns(tool_result_cache={})

        if self._is_cancellation_reply(user_text):
            self.pending_action = None
            cancellation_message = "Cancelled the pending action. No changes were made."
            self.history.append(
                Message(role="assistant", content=cancellation_message)
            )
            return cancellation_message

        reminder_message = (
            self._format_confirmation_message(
                pending_action.confirmation_preview
            )
        )
        self.history.append(Message(role="assistant", content=reminder_message))
        return reminder_message

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
            requires_confirmation=tool.requires_confirmation,
        )

    def _queue_pending_action(self, tool_call: ToolCall) -> str:
        """Store a destructive action and return its confirmation prompt."""

        confirmation_preview = self._build_confirmation_preview(tool_call)
        self.pending_action = PendingToolAction(
            tool_call=tool_call,
            confirmation_preview=confirmation_preview,
        )
        return self._format_confirmation_message(confirmation_preview)

    def _build_confirmation_preview(self, tool_call: ToolCall) -> str:
        """Render a deterministic preview for a pending action."""

        formatter = self.confirmation_formatters.get(tool_call.name)
        return (
            formatter(tool_call.arguments)
            if formatter is not None
            else self._default_confirmation_preview(tool_call)
        )

    @staticmethod
    def _format_confirmation_message(preview: str) -> str:
        """Wrap a pending-action preview in the standard confirm/cancel text."""

        return (
            "This action requires confirmation before I can continue.\n"
            f"{preview}\n"
            "Reply 'confirm' to continue or 'cancel' to stop."
        )

    def _default_confirmation_preview(self, tool_call: ToolCall) -> str:
        """Render a generic action preview from the tool name and arguments."""

        if not tool_call.arguments:
            return f"Pending action: {tool_call.name}"

        preview_lines = [f"Pending action: {tool_call.name}", "Arguments:"]
        for key in sorted(tool_call.arguments):
            preview_lines.append(
                f"- {key}: {self._serialize_confirmation_value(tool_call.arguments[key])}"
            )
        return "\n".join(preview_lines)

    @staticmethod
    def _serialize_confirmation_value(value: object) -> str:
        """Render a tool argument into a compact single-line preview."""

        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, default=str)
        except TypeError:
            return str(value)

    def _requires_confirmation(self, tool_call: ToolCall) -> bool:
        """Return whether a tool call should pause for user confirmation."""

        return tool_call.name in self.tools_requiring_confirmation

    @staticmethod
    def _is_confirmation_reply(user_text: str) -> bool:
        """Recognize explicit approval replies for a pending action."""

        normalized = " ".join(user_text.strip().lower().split())
        return normalized in {"confirm", "confirm it", "yes", "yes confirm", "proceed"}

    @staticmethod
    def _is_cancellation_reply(user_text: str) -> bool:
        """Recognize explicit cancellation replies for a pending action."""

        normalized = " ".join(user_text.strip().lower().split())
        return normalized in {"cancel", "no", "stop", "abort", "never mind"}

    @staticmethod
    def _side_effecting_tool_cache_key(tool_call: ToolCall) -> str | None:
        """Cache duplicate write-tool calls within a turn to avoid repeats."""

        if not ConversationService._is_side_effecting_tool(tool_call.name):
            return None

        try:
            serialized_args = json.dumps(
                tool_call.arguments,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except TypeError:
            serialized_args = str(tool_call.arguments)
        return f"{tool_call.name}:{serialized_args}"

    @staticmethod
    def _is_side_effecting_tool(tool_name: str) -> bool:
        """Heuristically identify tools that should not run twice by accident."""

        return tool_name.startswith(
            (
                "create_",
                "delete_",
                "update_",
                "modify_",
                "draft_",
                "send_",
            )
        )

    @staticmethod
    def _trim_text(text: str, max_chars: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= max_chars:
            return normalized
        if max_chars <= 3:
            return normalized[:max_chars]
        return normalized[: max_chars - 3].rstrip() + "..."
