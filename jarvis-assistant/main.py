from jarvis.assistant import Assistant


def main():
    assistant = Assistant()
    print("Jarvis is online. Type 'exit' to quit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nJarvis: Goodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Jarvis: Goodbye.")
            break

        reply = assistant.send(user_input)
        print(f"Jarvis: {reply}\n")


if __name__ == "__main__":
    main()
