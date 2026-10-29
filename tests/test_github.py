import json
import subprocess

import pytest

from foreman.github import FakeGitHub, GitHub, Issue
from foreman.util import ShResult


def write(repo, rel, text):
    p = repo.path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def fake(tmp_path, repo):
    return FakeGitHub(tmp_path / "gh", repo)


# ---- FakeGitHub ----

def test_issues_and_prs_share_one_number_sequence(fake, repo):
    repo.start_run("foreman/r1", "main")
    assert fake.create_issue("A", "body", ["foreman:feature"]) == 1
    assert fake.open_pr("foreman/r1", "PR", "text", ["feature"], closes=1) == 2
    assert fake.create_issue("B", "", []) == 3


def test_get_issue_round_trips_fields(fake):
    n = fake.create_issue("Title", "Body", ["foreman:bugfix"])
    assert fake.get_issue(n) == Issue(n, "Title", "Body", ["foreman:bugfix"])


def test_comment_is_stored_on_the_issue(fake, tmp_path):
    n = fake.create_issue("T", "b", [])
    fake.comment(n, "gate passed")
    data = json.loads((tmp_path / "gh/issues" / f"{n}.json").read_text())
    assert data["comments"] == ["gate passed"]


def test_pr_file_records_closes_line_and_labels(fake, repo, tmp_path):
    repo.start_run("foreman/r1", "main")
    n = fake.create_issue("T", "b", [])
    pr = fake.open_pr("foreman/r1", "Title", "Summary", ["feature"], closes=n)
    data = json.loads((tmp_path / "gh/prs" / f"{pr}.json").read_text())
    assert f"Closes #{n}" in data["body"] and data["labels"] == ["feature"]


def test_new_pr_is_open(fake, repo):
    repo.start_run("foreman/r1", "main")
    pr = fake.open_pr("foreman/r1", "T", "b", [], closes=None)
    assert fake.pr_state(pr).state == "open"


def test_merge_does_a_real_git_merge_into_main(fake, repo):
    repo.start_run("foreman/r1", "main")
    write(repo, "src/a.py", "x = 1\n")
    repo.commit("work")
    pr = fake.open_pr("foreman/r1", "T", "b", [], closes=None)
    fake.merge(pr)
    st = fake.pr_state(pr)
    assert st.state == "merged" and st.merge_sha == repo.head()
    assert repo.branch() == "main" and (repo.path / "src/a.py").exists()
    assert fake.merged_commit(pr) == st.merge_sha


def test_merged_commit_of_open_pr_raises(fake, repo):
    repo.start_run("foreman/r1", "main")
    pr = fake.open_pr("foreman/r1", "T", "b", [], closes=None)
    with pytest.raises(RuntimeError, match="not merged"):
        fake.merged_commit(pr)


def test_close_without_merge_records_reviewer_comment(fake, repo):
    repo.start_run("foreman/r1", "main")
    pr = fake.open_pr("foreman/r1", "T", "b", [], closes=None)
    fake.close(pr, "needs rate limiting")
    st = fake.pr_state(pr)
    assert st.state == "closed" and st.comments == ["needs rate limiting"]


def test_push_and_ensure_repo_are_harmless(fake):
    fake.push("foreman/r1")
    fake.push_tag("v1")
    fake.ensure_repo("url-shortener")


# ---- GitHub (gh CLI), with a recording runner ----

class Recorder:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, cmd, cwd, input=None, **kw):
        self.calls.append(cmd)
        for prefix, out in self.replies.items():
            if " ".join(cmd).startswith(prefix):
                return ShResult(0, out)
        return ShResult(0, "")


def gh_with(replies, repo):
    rec = Recorder(replies)
    return GitHub(repo, runner=rec), rec


def test_create_issue_builds_gh_argv_and_parses_number(repo):
    gh, rec = gh_with({"gh issue create": "https://github.com/o/r/issues/12\n"}, repo)
    assert gh.create_issue("T", "B", ["foreman:feature"]) == 12
    assert ["gh", "issue", "create", "--title", "T", "--body", "B", "--label", "foreman:feature"] in rec.calls
    assert ["gh", "label", "create", "foreman:feature", "--force"] in rec.calls


def test_get_issue_parses_json(repo):
    payload = json.dumps({"number": 5, "title": "T", "body": "B", "labels": [{"name": "foreman:spike"}]})
    gh, _ = gh_with({"gh issue view": payload}, repo)
    assert gh.get_issue(5) == Issue(5, "T", "B", ["foreman:spike"])


def test_open_pr_appends_closes_and_parses_number(repo):
    gh, rec = gh_with({"gh pr create": "https://github.com/o/r/pull/9\n"}, repo)
    assert gh.open_pr("foreman/r1", "Title", "Body", ["feature"], closes=4) == 9
    create = next(c for c in rec.calls if c[:3] == ["gh", "pr", "create"])
    assert create[create.index("--body") + 1].endswith("Closes #4")
    assert create[create.index("--head") + 1] == "foreman/r1"


@pytest.mark.parametrize("raw,expected", [("OPEN", "open"), ("MERGED", "merged"), ("CLOSED", "closed")])
def test_pr_state_maps_github_states(repo, raw, expected):
    payload = json.dumps({
        "state": raw, "mergeCommit": {"oid": "abc"} if raw == "MERGED" else None,
        "comments": [{"body": "c1"}], "reviews": [{"body": "r1"}, {"body": ""}],
    })
    gh, _ = gh_with({"gh pr view": payload}, repo)
    st = gh.pr_state(3)
    assert st.state == expected and st.comments == ["c1", "r1"]
    assert st.merge_sha == ("abc" if raw == "MERGED" else None)


def test_gh_failure_raises_with_output(repo):
    def failing(cmd, cwd, input=None, **kw):
        return ShResult(1, "HTTP 401")
    with pytest.raises(RuntimeError, match="HTTP 401"):
        GitHub(repo, runner=failing).comment(1, "x")


def test_login_asks_gh_for_the_authenticated_user(repo):
    gh, rec = gh_with({"gh api user": "octocat\n"}, repo)
    assert gh.login() == "octocat"
    assert ["gh", "api", "user", "--jq", ".login"] in rec.calls


def test_ensure_repo_creates_and_protects_main_when_missing(repo):
    def runner(cmd, cwd, input=None, **kw):
        runner.calls.append(cmd)
        return ShResult(1, "not found") if cmd[:3] == ["gh", "repo", "view"] else ShResult(0, "")
    runner.calls = []
    GitHub(repo, runner=runner).ensure_repo("o/url-shortener")
    flat = [" ".join(c) for c in runner.calls]
    assert any(c.startswith("gh repo create o/url-shortener") for c in flat)
    assert any("branches/main/protection" in c for c in flat)


def test_open_pr_targets_main_unless_another_base_was_confirmed(repo):
    gh, rec = gh_with({"gh pr create": "https://github.com/o/r/pull/9\n"}, repo)
    gh.open_pr("foreman/r1", "T", "B", [], closes=None)
    gh.base = "develop"
    gh.open_pr("foreman/r1", "T", "B", [], closes=None)
    bases = [c[c.index("--base") + 1] for c in rec.calls if c[:3] == ["gh", "pr", "create"]]
    assert bases == ["main", "develop"]


def test_merged_commit_syncs_the_confirmed_base_branch(repo, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "develop", str(origin)], check=True)
    repo.git("remote", "add", "origin", str(origin))
    repo.git("push", "-q", "origin", "main:develop")
    payload = json.dumps({"state": "MERGED", "mergeCommit": {"oid": "abc"}, "comments": [], "reviews": []})
    gh, _ = gh_with({"gh pr view": payload}, repo)
    gh.base = "develop"
    assert gh.merged_commit(3) == "abc"
    assert repo.branch() == "develop"
