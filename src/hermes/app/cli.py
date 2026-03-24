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
            # The CLI is responsible only for collecting raw user text.
            user_text = input(prompt).strip()
        except EOFError:
            # EOF usually means the user closed stdin; exit cleanly.
            print()
            return 0
        except KeyboardInterrupt:
            # Ctrl+C at the prompt exits the program.
            print("\nGoodbye.")
            return 130

        if not user_text:
            continue

        if user_text.lower() in {"exit", "quit"}:
            print("Goodbye.")
            return 0

        try:
            # Any model/tool orchestration happens in the service layer.
            response = conversation_service.handle_user_input(user_text)
        except KeyboardInterrupt:
            # Ctrl+C during a single turn cancels that turn but keeps the CLI open.
            print("\nCancelled.")
            continue

        if response:
            print(response)
