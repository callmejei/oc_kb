"""D-064: policy-level conflicts are detected and noted in code, narrowly."""

from kb.agent.answer import answer
from kb.agent.conflict import Src, find_conflicts, note_for
from kb.ports.types import Chunk, ScoredChunk

GROUP = (
    "Land for development, LTV - first facility = 45%. Land for development, LTV - third or more facilities = 15%. "
    "Commercial office, LTV - first facility = 55%."
)
CORP = (
    "Land for development, **LTV - first facility** = 35%. Land for development, **LTV - third or more facilities** "
    "= 15%. Commercial office, **LTV - first facility** = 55%."
)
SRC = [Src("1", GROUP, "shared", "Group RAS"), Src("2", CORP, "corporate", "CRE policy")]
Q = "What is the maximum lending ratio for a development site on a first facility?"


def test_group_value_answer_gets_both_values_and_the_stricter_one():
    (c,) = find_conflicts("It is 45% [1].", SRC, Q)
    assert c.by_value == {"1": "45%", "2": "35%"} and c.stricter == "35%"
    note = note_for(c)
    assert "[1]" in note and "[2]" in note and "The stricter limit, 35%, applies." in note


def test_bu_value_answer_also_shows_the_group_ceiling():
    (c,) = find_conflicts("It is 35% [2].", SRC, Q)
    assert c.by_value == {"2": "35%", "1": "45%"}


def test_same_value_or_different_item_is_not_a_conflict():
    assert find_conflicts("Commercial office first facility is 55% [1].", SRC) == []
    assert find_conflicts("Third or more facilities: 15% [2].", SRC) == []


def test_two_business_units_are_not_compared():
    other = [Src("1", GROUP.replace("45%", "50%"), "retail", "x"), Src("2", CORP, "corporate", "y")]
    assert find_conflicts("It is 50% [1].", other, Q) == []


def test_unknown_direction_shows_both_without_a_verdict():
    s = [Src("1", "Fee waiver, share of clients = 40%.", "shared", "a"),
         Src("2", "Fee waiver, share of clients = 25%.", "wealth", "b")]  # fmt: skip
    (c,) = find_conflicts("It is 40% [1].", s, "What share of clients get a fee waiver?")
    assert c.stricter is None and "applies" not in note_for(c)


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def chat(self, messages, *, temperature=0.0, max_tokens=None):  # type: ignore[no-untyped-def]
        return self.reply


def chunk(n: int, text: str, bu: str) -> ScoredChunk:
    c = Chunk(f"c{n}", f"d{n}", f"d{n}:v1", text, (), ["G"], bu, True, True, page=1, metadata={"title": f"T{n}"})
    return ScoredChunk(c, 1.0, "rerank")


def test_answer_appends_the_note_and_cites_both_sources():
    ctx = [chunk(1, GROUP, "shared"), chunk(2, CORP, "corporate")]
    a = answer(Q, ctx, FakeLLM("The maximum lending ratio is 45% [1]."))
    assert a.grounded and "The stricter limit, 35%, applies." in a.text
    assert sorted(c.n for c in a.citations) == [1, 2] and a.conflicts[0]["stricter"] == "35%"


def test_answer_already_stating_both_values_is_left_alone():
    ctx = [chunk(1, GROUP, "shared"), chunk(2, CORP, "corporate")]
    text = "The corporate limit is 35% [2]. The group allows up to 45% [1]."
    assert answer(Q, ctx, FakeLLM(text)).text == text
