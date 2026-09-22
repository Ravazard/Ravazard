import anthropic

from .config import ANTHROPIC_API_KEY, MODEL, SYSTEM_PROMPT
from .tools import TOOL_FUNCTIONS, TOOL_SPECS


class Assistant:
    def __init__(self):
        if not ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env "
                "and add your key."
            )
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.history = []

    def _run_tool(self, name: str, tool_input: dict) -> str:
        func = TOOL_FUNCTIONS.get(name)
        if func is None:
            return f"Unknown tool: {name}"
        try:
            return func(**tool_input)
        except Exception as exc:  # tool failures shouldn't crash the loop
            return f"Tool '{name}' failed: {exc}"

    def send(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})

        while True:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOL_SPECS,
                messages=self.history,
            )

            self.history.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
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
