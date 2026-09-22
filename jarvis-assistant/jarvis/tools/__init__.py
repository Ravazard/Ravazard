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
]

TOOL_FUNCTIONS = {
    "get_weather": get_weather,
    "web_search": web_search,
}
