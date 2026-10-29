import pytest

from foreman.commits import STAGES, clean, fallback, lint

GOOD = (
    "feat: add click counting\n\n"
    "Count each redirect so the owner can see traffic. Before, the\n"
    "redirect did not record anything.\n\n"
    "- add a counter to the link record\n"
    "- increment it on every redirect"
)


def problems(message):
    return " | ".join(lint(message))


def test_a_conventional_message_passes():
    assert lint(GOOD) == []


def test_scope_and_breaking_marker_are_allowed():
    assert lint("fix(api)!: reject unknown short codes") == []


@pytest.mark.parametrize("kind", ["feat", "fix", "docs", "style", "refactor", "perf", "test", "chore", "ci"])
def test_every_listed_type_is_accepted(kind):
    assert lint(f"{kind}: update the thing") == []


@pytest.mark.parametrize("subject", [
    "add click counting",              # no type
    "feature: add click counting",     # not a listed type
    "Feat: add click counting",        # type must be lowercase
    "feat(Api): add click counting",   # scope must be lowercase
    "feat:add click counting",         # missing space
    "feat: ",                          # no description
])
def test_the_subject_must_be_type_scope_colon_description(subject):
    assert "type" in problems(subject)


def test_subject_must_be_under_50_characters():
    assert lint("feat: " + "x" * 43) == []                      # 49
    assert "under 50" in problems("feat: " + "x" * 44)          # 50


def test_subject_must_not_end_with_a_period():
    assert "period" in problems("feat: add click counting.")


@pytest.mark.parametrize("subject", ["feat: added click counting", "feat: adds click counting", "feat: adding click counting"])
def test_subject_must_be_imperative(subject):
    assert "imperative" in problems(subject)


@pytest.mark.parametrize("subject", [
    "fix: address redirect loops", "chore: bypass the cache", "feat: embed the diagram", "docs: process the data",
    "feat: use status codes", "feat: focus the search box",
])
def test_imperative_check_does_not_flag_ordinary_verbs(subject):
    assert lint(subject) == []


def test_bullets_in_the_body_must_be_imperative_too():
    assert "imperative" in problems("feat: add counting\n\nBody.\n\n- added a counter")


def test_body_must_be_separated_by_a_blank_line():
    assert "blank line" in problems("feat: add counting\nBody starts too early.")


def test_body_lines_are_wrapped_at_72():
    assert lint("feat: add counting\n\n" + "a" * 72) == []
    assert "72" in problems("feat: add counting\n\n" + "a" * 73)


def test_an_empty_message_is_rejected():
    assert "empty" in problems("  \n")


def test_clean_unwraps_a_fenced_message():
    assert clean("```\nfeat: add counting\n```\n") == "feat: add counting"
    assert clean("```text\nfeat: add counting\n```") == "feat: add counting"
    assert clean("  feat: add counting \n") == "feat: add counting"


def test_every_stage_has_a_valid_fallback_message():
    for name in STAGES:
        assert lint(fallback(name)) == [], name


def test_fallback_uses_the_stage_name_before_the_slash_and_copes_with_unknown_stages():
    assert fallback("explore/option-2") == fallback("explore")
    assert fallback("implement").startswith("feat: ")
    assert fallback("tests").startswith("test: ")
    assert lint(fallback("something_new")) == []
