import pytest

from foreman.gitops import Change, GitRepo, RepoError


def write(repo, rel, text):
    p = repo.path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_init_creates_main_with_initial_commit(repo):
    assert repo.branch() == "main"
    assert len(repo.head()) == 40


def test_start_run_branches_from_ref(repo):
    base = repo.head()
    assert repo.start_run("foreman/r1", "main") == base
    assert repo.branch() == "foreman/r1"


def test_start_run_refuses_dirty_tree(repo):
    write(repo, "x.txt", "dirty")
    with pytest.raises(RepoError, match="dirty"):
        repo.start_run("foreman/r1", "main")


def test_start_run_refuses_existing_branch(repo):
    repo.start_run("foreman/r1", "main")
    repo.git("checkout", "-q", "main")
    with pytest.raises(RepoError, match="already exists"):
        repo.start_run("foreman/r1", "main")


def test_changes_reports_added_modified_deleted(repo):
    write(repo, "keep.txt", "1")
    write(repo, "gone.txt", "1")
    repo.commit("seed")
    write(repo, "keep.txt", "2")
    (repo.path / "gone.txt").unlink()
    write(repo, "new/dir/a.py", "x")
    assert sorted(repo.changes(), key=lambda c: c.path) == [
        Change("D", "gone.txt"),
        Change("M", "keep.txt"),
        Change("A", "new/dir/a.py"),
    ]


def test_commit_returns_new_head_and_leaves_tree_clean(repo):
    write(repo, "a.txt", "a")
    sha = repo.commit("add a")
    assert sha == repo.head() and repo.changes() == []


def test_commit_allows_empty(repo):
    before = repo.head()
    assert repo.commit("empty") != before


def test_reset_hard_reverts_edits_and_removes_untracked(repo):
    repo.start_run("foreman/r1", "main")
    write(repo, "a.txt", "a")
    repo.commit("a")
    write(repo, "a.txt", "changed")
    write(repo, "junk/b.txt", "b")
    repo.reset_hard()
    assert repo.changes() == [] and (repo.path / "a.txt").read_text() == "a"


def test_reset_hard_to_earlier_sha(repo):
    repo.start_run("foreman/r1", "main")
    first = repo.commit("one")
    write(repo, "a.txt", "a")
    repo.commit("two")
    repo.reset_hard(first)
    assert repo.head() == first and not (repo.path / "a.txt").exists()


def test_reset_hard_refuses_outside_foreman_branch(repo):
    write(repo, "precious.txt", "mine")
    with pytest.raises(RepoError, match="foreman/"):
        repo.reset_hard()
    assert (repo.path / "precious.txt").exists()


def test_added_text_has_added_lines_and_new_file_contents(repo):
    write(repo, "a.txt", "old\n")
    repo.commit("seed")
    write(repo, "a.txt", "old\nadded-line\n")
    write(repo, "b.txt", "brand-new-content\n")
    text = repo.added_text()
    assert "added-line" in text and "brand-new-content" in text


def test_added_text_skips_binary_files(repo):
    (repo.path / "blob.bin").write_bytes(bytes(range(256)) * 4)
    assert repo.added_text() == ""


def test_diff_stat_and_text_against_base(repo):
    repo.start_run("foreman/r1", "main")
    base = repo.head()
    write(repo, "src/a.py", "print(1)\n")
    repo.commit("a")
    assert "src/a.py" in repo.diff_stat(base)
    assert "print(1)" in repo.diff_text(base, ["src/a.py"])


def test_tree_lists_tracked_files(repo):
    write(repo, "src/a.py", "x")
    repo.commit("a")
    assert "src/a.py" in repo.tree().splitlines()


def test_snapshot_and_restore_round_trip(repo):
    repo.start_run("foreman/r1", "main")
    write(repo, "a.txt", "keep")
    repo.commit("a")
    write(repo, "a.txt", "edited")
    write(repo, "n/b.txt", "new")
    snap = repo.snapshot()
    repo.reset_hard()
    assert not (repo.path / "n/b.txt").exists()
    repo.restore(snap)
    assert (repo.path / "a.txt").read_text() == "edited"
    assert (repo.path / "n/b.txt").read_text() == "new"
