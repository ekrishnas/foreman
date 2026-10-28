from pathlib import Path

import pytest

from foreman.gitops import GitRepo

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo(tmp_path):
    return GitRepo.init(tmp_path / "target")
