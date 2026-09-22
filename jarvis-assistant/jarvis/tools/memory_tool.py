from .. import memory


def remember_fact(key: str, value: str) -> str:
    facts = memory.load_facts()
    facts[key] = value
    memory.save_facts(facts)
    return f"Got it, I'll remember that {key} = {value}."
