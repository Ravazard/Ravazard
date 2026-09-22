import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WEATHER_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    80: "rain showers",
    95: "thunderstorm",
}


def get_weather(location: str) -> str:
    try:
        geo = requests.get(
            GEOCODE_URL, params={"name": location, "count": 1}, timeout=10
        ).json()
    except requests.RequestException as exc:
        return f"Could not reach the weather service: {exc}"

    results = geo.get("results")
    if not results:
        return f"Could not find a location matching '{location}'."

    place = results[0]
    lat, lon = place["latitude"], place["longitude"]
    place_name = place.get("name", location)

    try:
        forecast = requests.get(
            FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code,relative_humidity_2m",
                "timezone": "auto",
            },
            timeout=10,
        ).json()
    except requests.RequestException as exc:
        return f"Could not reach the weather service: {exc}"

    current = forecast.get("current")
    if not current:
        return f"No current weather data available for {place_name}."

    temp = current.get("temperature_2m")
    humidity = current.get("relative_humidity_2m")
    code = current.get("weather_code")
    condition = WEATHER_CODES.get(code, "unknown conditions")

    return (
        f"{place_name}: {temp}°C, {condition}, {humidity}% humidity."
    )
