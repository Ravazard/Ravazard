# Jarvis Assistant — Stage 1

A text-based personal assistant powered by Claude, with tool calling.
This is stage 1 of a JARVIS-style build: prove the "brain" works
(persona + tool use) before adding memory, voice, and more integrations.

## What's here

- `jarvis/assistant.py` — conversation loop, handles Claude's tool-use
  requests and feeds results back until it has a final answer.
- `jarvis/config.py` — persona system prompt, model selection.
- `jarvis/tools/` — two working tools, no API key required beyond Anthropic's:
  - `get_weather(location)` — via Open-Meteo (free, no key).
  - `web_search(query)` — via DuckDuckGo's Instant Answer API (free, no
    key, but limited to short factual/definition-style answers — swap in
    Tavily/Brave/SerpAPI here for real search results).
- `main.py` — a simple REPL you run in the terminal.

## Setup

```bash
cd jarvis-assistant
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env and add your ANTHROPIC_API_KEY
python main.py
```

## Try it

```
You: what's the weather in Mumbai?
You: who won the nobel prize in physics in 2024?
You: what can you do?
```

## Roadmap (next stages)

1. ✅ Text chatbot with persona + tools (this stage)
2. Persistent memory across sessions (vector DB or simple JSON facts store)
3. Voice input (Whisper) / output (Piper or ElevenLabs) — still push-to-talk
4. Wake-word activation ("Hey Jarvis") for hands-free use
5. More tools: calendar, smart home (Home Assistant), email, code execution
6. Run it always-on on a dedicated device (e.g. Raspberry Pi)
