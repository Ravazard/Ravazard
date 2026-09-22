import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FACTS_FILE = DATA_DIR / "facts.json"
HISTORY_FILE = DATA_DIR / "history.json"

# Keep saved conversation history bounded so context/token usage doesn't
# grow unboundedly across many sessions.
MAX_HISTORY_MESSAGES = 40


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_facts() -> dict:
    if not FACTS_FILE.exists():
        return {}
    with open(FACTS_FILE) as f:
        return json.load(f)


def save_facts(facts: dict) -> None:
    _ensure_data_dir()
    with open(FACTS_FILE, "w") as f:
        json.dump(facts, f, indent=2)


def load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE) as f:
        return json.load(f)


def save_history(history: list) -> None:
    _ensure_data_dir()
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[-MAX_HISTORY_MESSAGES:], f, indent=2)
