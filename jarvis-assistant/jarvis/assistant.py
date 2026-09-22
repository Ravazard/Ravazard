import anthropic

from . import memory
from .config import ANTHROPIC_API_KEY, MODEL, build_system_prompt
from .tools import TOOL_FUNCTIONS, TOOL_SPECS


class Assistant:
    def __init__(self):
        if not ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env "
                "and add your key."
            )
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.history = memory.load_history()

    def _run_tool(self, name: str, tool_input: dict) -> str:
        func = TOOL_FUNCTIONS.get(name)
        if func is None:
            return f"Unknown tool: {name}"
        try:
            return func(**tool_input)
        except Exception as exc:  # tool failures shouldn't crash the loop
            return f"Tool '{name}' failed: {exc}"

    def reset_memory(self) -> None:
        memory.save_facts({})
        memory.save_history([])
        self.history = []

    def send(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})
        if len(self.history) > memory.MAX_HISTORY_MESSAGES:
            self.history = self.history[-memory.MAX_HISTORY_MESSAGES :]

        while True:
            system_prompt = build_system_prompt(memory.load_facts())
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=system_prompt,
                tools=TOOL_SPECS,
                messages=self.history,
            )

            self.history.append(
                {
                    "role": "assistant",
                    "content": [block.model_dump() for block in response.content],
                }
            )

            if response.stop_reason != "tool_use":
                memory.save_history(self.history)
                return "".join(
                    block.text for block in response.content if block.type == "text"
                )

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = self._run_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result),
                    }
                )

            self.history.append({"role": "user", "content": tool_results})
