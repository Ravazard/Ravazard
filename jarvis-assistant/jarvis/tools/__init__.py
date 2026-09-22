from .memory_tool import remember_fact
from .weather import get_weather
from .web_search import web_search

TOOL_SPECS = [
    {
        "name": "get_weather",
        "description": (
            "Get the current weather and a short forecast for a location. "
            "Use this whenever the user asks about weather, temperature, "
            "or whether they need an umbrella/jacket."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name, e.g. 'Bengaluru' or 'New York'",
                },
            },
            "required": ["location"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web for a quick factual answer or summary. "
            "Use this for current events, definitions, or facts you are not "
            "confident about."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember_fact",
        "description": (
            "Save a fact about the user for future conversations - e.g. "
            "their name, timezone, or a preference. Use this whenever the "
            "user tells you something worth remembering long-term."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": (
                        "Short label for the fact, e.g. 'name', 'timezone', "
                        "'favorite_language'"
                    ),
                },
                "value": {
                    "type": "string",
                    "description": "The fact's value",
                },
            },
            "required": ["key", "value"],
        },
    },
]

TOOL_FUNCTIONS = {
    "get_weather": get_weather,
    "web_search": web_search,
    "remember_fact": remember_fact,
}
