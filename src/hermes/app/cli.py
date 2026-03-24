from hermes.services.conversation_service import ConversationService


def run_cli(
    conversation_service: ConversationService,
    *,
    prompt: str = "hermes> ",
) -> int:
    """
    Run a simple terminal loop.

    The CLI only owns terminal interaction. Conversation orchestration belongs
    in the injected service.
    """

    while True:
        try:
            user_text = input(prompt).strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("\nGoodbye.")
            return 130

        if not user_text:
            continue

        if user_text.lower() in {"exit", "quit"}:
            print("Goodbye.")
            return 0

        try:
            response = conversation_service.handle_user_input(user_text)
        except KeyboardInterrupt:
            print("\nCancelled.")
            continue

        if response:
            print(response)
