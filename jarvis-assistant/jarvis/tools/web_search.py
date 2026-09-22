import requests

SEARCH_URL = "https://api.duckduckgo.com/"


def web_search(query: str) -> str:
    """Quick factual lookup via DuckDuckGo's Instant Answer API.

    This only returns short instant-answer style results (definitions,
    disambiguation, infobox summaries) - not full search results. Swap in
    a real search API (Tavily, Brave Search, SerpAPI) here once you need
    broader coverage.
    """
    try:
        resp = requests.get(
            SEARCH_URL,
            params={
                "q": query,
                "format": "json",
                "no_html": 1,
                "skip_disambig": 1,
            },
            timeout=10,
        ).json()
    except requests.RequestException as exc:
        return f"Could not reach the search service: {exc}"

    abstract = resp.get("AbstractText")
    if abstract:
        source = resp.get("AbstractSource", "")
        return f"{abstract} (source: {source})" if source else abstract

    related = resp.get("RelatedTopics") or []
    snippets = [t["Text"] for t in related if isinstance(t, dict) and t.get("Text")]
    if snippets:
        return "Related info:\n" + "\n".join(f"- {s}" for s in snippets[:3])

    return f"No quick answer found for '{query}'. Try rephrasing the query."
