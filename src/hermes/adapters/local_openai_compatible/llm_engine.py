from __future__ import annotations

import json
from typing import Any
from urllib import error, request

from pydantic import SecretStr

from hermes.config import Settings, get_settings
from hermes.ports.llm import LLM, LLMResponse, Message, Tool, ToolCall, Usage


class LocalOpenAICompatibleLLM(LLM):
    """
    LLM adapter for a local OpenAI-compatible server such as llama.cpp.

    By default this targets `http://127.0.0.1:8080/v1/chat/completions`.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: int | None = None,
        api_key: str | None = None,
    ) -> None:
        """Configure a local OpenAI-compatible model endpoint."""

        resolved_settings = settings or get_settings()

        self.base_url = (
            base_url or resolved_settings.local_llm_base_url
        ).rstrip("/")
        self.model = model or resolved_settings.llm_model or "local-model"
        self.timeout_s = timeout_s or resolved_settings.llm_timeout_s
        self.api_key = api_key or self._read_secret(
            resolved_settings.local_llm_api_key
        )

    def generate(
        self,
        messages: list[Message],
        *,
        tools: list[Tool] | None = None,
    ) -> LLMResponse:
        """Send one chat-completions request and normalize the response."""

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._serialize_message(message) for message in messages],
        }

        if tools:
            # OpenAI-compatible tool calling expects a list of tool specs and
            # a tool selection mode.
            payload["tools"] = [self._serialize_tool(tool) for tool in tools]
            payload["tool_choice"] = "auto"

        response_data = self._post_json(
            f"{self.base_url}/chat/completions",
            payload,
        )
        return self._parse_chat_response(response_data)

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """POST JSON to the local server and return the decoded JSON body."""

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        http_request = request.Request(
            url=url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with request.urlopen(http_request, timeout=self.timeout_s) as response:
                response_body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Local LLM request failed with HTTP {exc.code}: {error_body}"
            ) from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"Could not reach local LLM server at {url}: {exc.reason}"
            ) from exc

        try:
            parsed: dict[str, Any] = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Local LLM server returned non-JSON response."
            ) from exc

        return parsed

    def _parse_chat_response(self, payload: dict[str, Any]) -> LLMResponse:
        """Convert the provider payload into the Hermes response contract."""

        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("Local LLM response did not include any choices.")

        message = choices[0].get("message") or {}
        usage_data = payload.get("usage")

        return LLMResponse(
            content=self._extract_content(message.get("content")),
            tool_calls=self._extract_tool_calls(message.get("tool_calls")),
            usage=self._extract_usage(usage_data),
        )

    @staticmethod
    def _serialize_message(message: Message) -> dict[str, Any]:
        """Convert a Hermes message into the provider wire format."""

        payload: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }

        if message.name is not None:
            payload["name"] = message.name
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id

        return payload

    @staticmethod
    def _serialize_tool(tool: Tool) -> dict[str, Any]:
        """Convert a Hermes tool definition into an OpenAI-style schema."""

        return {
            "type": tool.tool_type,
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }

    @staticmethod
    def _extract_content(content: Any) -> str | None:
        """Normalize provider content variants into plain assistant text."""

        if content is None:
            return None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts) or None
        return str(content)

    @staticmethod
    def _extract_tool_calls(raw_tool_calls: Any) -> list[ToolCall]:
        """Convert provider tool-call payloads into Hermes tool calls."""

        if not isinstance(raw_tool_calls, list):
            return []

        tool_calls: list[ToolCall] = []
        for raw_tool_call in raw_tool_calls:
            if not isinstance(raw_tool_call, dict):
                continue

            function_data = raw_tool_call.get("function") or {}
            arguments = function_data.get("arguments", {})
            # Providers often return arguments as a JSON string rather than a
            # decoded dict, so normalize here before returning the tool call.
            parsed_arguments = LocalOpenAICompatibleLLM._parse_tool_arguments(
                arguments
            )

            tool_calls.append(
                ToolCall(
                    id=str(raw_tool_call.get("id", "")),
                    name=str(function_data.get("name", "")),
                    arguments=parsed_arguments,
                )
            )

        return tool_calls

    @staticmethod
    def _parse_tool_arguments(arguments: Any) -> dict[str, object]:
        """Parse tool arguments that may arrive as JSON text or a dict."""

        if isinstance(arguments, dict):
            return arguments
        if not isinstance(arguments, str):
            return {}

        stripped = arguments.strip()
        if not stripped:
            return {}

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {"raw_arguments": stripped}

        return parsed if isinstance(parsed, dict) else {"value": parsed}

    @staticmethod
    def _extract_usage(usage_data: Any) -> Usage | None:
        """Convert provider usage metadata into the Hermes usage type."""

        if not isinstance(usage_data, dict):
            return None

        return Usage(
            prompt_tokens=int(usage_data.get("prompt_tokens", 0)),
            completion_tokens=int(usage_data.get("completion_tokens", 0)),
            total_tokens=int(usage_data.get("total_tokens", 0)),
        )

    @staticmethod
    def _read_secret(secret: SecretStr | str | None) -> str | None:
        """Return a plain string value from a secret-like config field."""

        if secret is None:
            return None
        if isinstance(secret, SecretStr):
            return secret.get_secret_value()
        return secret
