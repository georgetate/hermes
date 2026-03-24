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
    message_history: list[Message] = field(default_factory=list)
    system_prompt: str | None = None
    max_tool_rounds: int = 8

    def __post_init__(self) -> None:
        if self.system_prompt and not any(
            message.role == "system" for message in self.message_history
        ):
            self.message_history.insert(
                0,
                Message(role="system", content=self.system_prompt),
            )

    def register_tool(self, tool: Tool, handler: ToolHandler) -> None:
        self.tools.append(tool)
        self.tool_handlers[tool.name] = handler
    
    def handle_user_input(self, user_text: str) -> str:
        self.message_history.append(Message(role="user", content=user_text))

        for _ in range(self.max_tool_rounds):
            response = self.llm.generate(
                self.message_history,
                tools=self.tools or None,
            )

            final_response = self._record_llm_response(response)
            if not response.tool_calls:
                return final_response

            for tool_call in response.tool_calls:
                tool_output = self._execute_tool_call(tool_call)
                self.message_history.append(
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
            self.message_history.append(
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
