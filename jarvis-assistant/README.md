# Jarvis Assistant — Stage 2

A text-based personal assistant powered by Claude, with tool calling and
persistent memory. Stage 1 proved the "brain" works (persona + tool use);
this stage adds memory that survives across sessions, before voice and
more integrations.

## What's here

- `jarvis/assistant.py` — conversation loop, handles Claude's tool-use
  requests, feeds results back until it has a final answer, and persists
  history/facts to disk after each exchange.
- `jarvis/config.py` — persona system prompt (dynamically includes
  remembered facts about the user), model selection.
- `jarvis/memory.py` — loads/saves `data/facts.json` (key-value facts
  about the user) and `data/history.json` (recent conversation, capped at
  `MAX_HISTORY_MESSAGES` messages so context doesn't grow unbounded).
- `jarvis/tools/` — three working tools, no API key required beyond
  Anthropic's:
  - `get_weather(location)` — via Open-Meteo (free, no key).
  - `web_search(query)` — via DuckDuckGo's Instant Answer API (free, no
    key, but limited to short factual/definition-style answers — swap in
    Tavily/Brave/SerpAPI here for real search results).
  - `remember_fact(key, value)` — saves a fact about the user for future
    sessions (e.g. their name, timezone, preferences).
- `main.py` — a simple REPL you run in the terminal. Type `/reset` to wipe
  all remembered facts and conversation history.

Memory lives in `jarvis-assistant/data/` (gitignored — it's your personal
data, not something to commit).

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
You: my name is Anirudh and I'm in Bengaluru
You: what's the weather where I live?
You: exit
```
Restart `python main.py` and ask "what's my name?" — it remembers, because
`facts.json` and `history.json` persisted to disk.

## Roadmap (next stages)

1. ✅ Text chatbot with persona + tools
2. ✅ Persistent memory across sessions (JSON facts store + saved history)
3. Voice input (Whisper) / output (Piper or ElevenLabs) — still push-to-talk
4. Wake-word activation ("Hey Jarvis") for hands-free use
5. More tools: calendar, smart home (Home Assistant), email, code execution
6. Run it always-on on a dedicated device (e.g. Raspberry Pi)
