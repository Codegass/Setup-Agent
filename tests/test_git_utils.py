import pytest

from sag.config.git_utils import extract_project_name_from_url as legacy_extract_project_name
from sag.utils.git_utils import extract_project_name_from_url


@pytest.mark.parametrize(
    ("repo_url", "expected"),
    [
        ("https://github.com/org/repo.git", "repo"),
        ("git@github.com:org/repo.git", "repo"),
        ("https://dev.azure.com/org/project/_git/service", "service"),
        ("/Users/example/projects/local-repo", "local-repo"),
        ("C:\\Users\\example\\repo-name", "repo-name"),
    ],
)
def test_extract_project_name_from_url(repo_url, expected):
    assert extract_project_name_from_url(repo_url) == expected


def test_extract_project_name_rejects_empty_url():
    with pytest.raises(ValueError, match="cannot be empty"):
        extract_project_name_from_url("")


def test_legacy_config_git_utils_import_still_works():
    assert legacy_extract_project_name("https://github.com/org/repo.git") == "repo"
