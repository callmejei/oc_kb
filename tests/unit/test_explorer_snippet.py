from kb.gateway.routes_explorer import SNIPPET, snippet


def test_snippet_starts_near_the_first_query_term():
    text = "x" * 1000 + " the internal codename is COPPERFINCH " + "y" * 1000
    s = snippet(text, "What is the internal codename?")
    assert "COPPERFINCH" in s and s.startswith("…") and s.endswith("…") and len(s) <= SNIPPET + 2


def test_snippet_without_a_match_is_the_start():
    assert snippet("short text", "zzzz") == "short text"
