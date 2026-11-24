"""``HttpGitHubClient`` against recorded-shape responses (httpx MockTransport). The JSON
shapes follow GitHub REST v2022-11-28; a read-only live check of the shapes is in
docs/evidence/m5. This is not a live API test."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from app.connectors.github import (
    BlobUnavailable,
    CollaboratorsUnavailable,
    GitHubAuthError,
    GitHubUnavailable,
    HttpGitHubClient,
    RepositoryMoved,
    RepositoryNotAccessible,
    TreeTruncated,
    load_github_config,
)

SHA = "a" * 40


def client(handler) -> HttpGitHubClient:  # type: ignore[no-untyped-def]
    return HttpGitHubClient("t0ken", transport=httpx.MockTransport(handler))


def test_repository_collaborators_tree_and_blob() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["authorization"] == "Bearer t0ken"
        assert request.headers["x-github-api-version"] == "2022-11-28"
        path = request.url.path
        if path == "/repos/o/r":
            return httpx.Response(
                200, json={"full_name": "o/r", "visibility": "private", "default_branch": "dev"}
            )
        if path == "/repos/o/r/collaborators":
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json=[{"login": "Bob", "role_name": "read"}])
            return httpx.Response(
                200,
                json=[
                    {"login": "Alice", "permissions": {"admin": True, "pull": True}},
                    {"login": "Carol", "permissions": {"pull": True, "triage": True}},
                    {"login": "Nobody", "permissions": {}},
                ],
                headers={
                    "Link": '<https://api.github.com/repos/o/r/collaborators?page=2>; rel="next"'
                },
            )
        if path == "/repos/o/r/commits/dev":
            return httpx.Response(200, json={"sha": SHA})
        if path == f"/repos/o/r/git/trees/{SHA}":
            assert request.url.params["recursive"] == "1"
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {"path": "a.py", "mode": "100644", "type": "blob", "sha": SHA, "size": 3},
                        {"path": "d", "mode": "040000", "type": "tree", "sha": SHA},
                    ],
                },
            )
        if path == f"/repos/o/r/git/blobs/{SHA}":
            return httpx.Response(
                200, json={"encoding": "base64", "content": base64.b64encode(b"x=1").decode()}
            )
        return httpx.Response(500)

    c = client(handler)
    repo = c.get_repository("o/r")
    assert (repo.full_name, repo.visibility, repo.default_branch) == ("o/r", "private", "dev")
    collaborators = c.list_collaborators("o/r")
    assert [(x.login, x.permission) for x in collaborators] == [
        ("alice", "admin"),
        ("carol", "triage"),
        ("bob", "pull"),
    ]
    assert c.resolve_commit("o/r", "dev") == SHA
    tree = c.get_tree("o/r", SHA)
    assert [(e.path, e.type, e.size) for e in tree] == [("a.py", "blob", 3), ("d", "tree", None)]
    assert c.get_blob("o/r", SHA) == b"x=1"
    assert any("affiliation=all" in u for u in seen)


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (401, {}, GitHubAuthError),
        (404, {}, RepositoryNotAccessible),
        (403, {}, RepositoryNotAccessible),
        (403, {"x-ratelimit-remaining": "0"}, GitHubUnavailable),
        (301, {"location": "https://api.github.com/repositories/1"}, RepositoryMoved),
        (502, {}, GitHubUnavailable),
    ],
)
def test_repository_failures_are_typed(
    status: int, headers: dict[str, str], error: type[Exception]
) -> None:
    c = client(lambda r: httpx.Response(status, headers=headers, json={}))
    with pytest.raises(error):
        c.get_repository("o/r")


def test_collaborator_listing_without_push_access_fails() -> None:
    c = client(lambda r: httpx.Response(403, json={"message": "Must have push access"}))
    with pytest.raises(CollaboratorsUnavailable):
        c.list_collaborators("o/r")


def test_truncated_tree_and_bad_blob_fail() -> None:
    c = client(lambda r: httpx.Response(200, json={"truncated": True, "tree": []}))
    with pytest.raises(TreeTruncated):
        c.get_tree("o/r", SHA)
    c = client(lambda r: httpx.Response(200, json={"encoding": "none", "content": ""}))
    with pytest.raises(BlobUnavailable):
        c.get_blob("o/r", SHA)


def test_config_validates_repositories(tmp_path: Path) -> None:
    path = tmp_path / "gh.json"
    path.write_text(
        json.dumps(
            {
                "tenants": {
                    "t": {"auth": {"type": "token", "token": "x"}, "allowed_repositories": ["O/R"]}
                }
            }
        )
    )
    assert load_github_config(str(path))["t"].allowed_repositories == frozenset({"o/r"})
    path.write_text(
        json.dumps(
            {
                "tenants": {
                    "t": {"auth": {"type": "token", "token": "x"}, "allowed_repositories": ["bad"]}
                }
            }
        )
    )
    with pytest.raises(ValueError, match="bad repository"):
        load_github_config(str(path))
