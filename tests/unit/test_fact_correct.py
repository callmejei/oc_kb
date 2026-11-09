"""D-064: with must_cite, a correct figure from the wrong policy level does not count."""

from types import SimpleNamespace

from kb.evaluation.golden import doc_title, fact_correct

Q = {"facts": ["35%"], "must_cite": ["corp-cre-policy"]}
CRE = doc_title("corp-cre-policy")


def ans(text: str, *titles: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, citations=[SimpleNamespace(title=t) for t in titles])


def test_fact_and_required_citation():
    assert fact_correct(Q, ans("35% [1]; the group allows 45% [2].", CRE, "Group_Credit_Risk_Appetite_Statement"))


def test_fact_without_the_required_citation_fails():
    assert not fact_correct(Q, ans("Up to 45%, not 35% [1].", "Group_Credit_Risk_Appetite_Statement"))
    assert not fact_correct(Q, ans("45% [1].", CRE))


def test_without_must_cite_only_the_fact_counts():
    assert fact_correct({"facts": ["35%"]}, ans("35%"))
