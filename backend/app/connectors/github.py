"""GitHub REST client used by the repository sync (M5, ADR 0502).

Credentials are server-side only (NFR-05) and bound to a tenant by the operator, not
by tenant administrators. ``GITHUB_CONNECTOR_CONFIG`` names a JSON file

    {"tenants": {"<tenant-slug>": {
        "auth": {"type": "token", "token_file": "/run/eaios/connectors/github-token"}
              | {"type": "token", "token": "<fine-grained read-only token>"},
        "allowed_repositories": ["owner/repo", ...],
        "api_base_url": "https://api.github.com"}}}

A tenant administrator can add only a repository the operator listed for that tenant.
The file is re-read on every use, so removing a repository fails that source's next
sync before any GitHub request, and its content is then denied.

The token needs read access to the repository's **contents and metadata**, and must
be able to list collaborators (``GET /repos/{o}/{r}/collaborators``), which GitHub only
allows with push access or the fine-grained "Administration: read" permission. A token
that cannot list collaborators makes every sync fail (``collaborators_unavailable``):
without the principal list the application cannot know who may read the code.

The client never guesses. 401 -> ``GitHubAuthError``; 403/404 on the repository ->
``RepositoryNotAccessible`` (deleted, made private to this token, or access revoked:
GitHub does not say which, and all three deny); a truncated tree -> ``TreeTruncated``.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

API = "https://api.github.com"
MAX_BYTES = 10 * 1024 * 1024
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
PERMISSION_ORDER = ("admin", "maintain", "push", "triage", "pull")


class GitHubError(Exception):
    code = "github_error"


class GitHubNotConfigured(GitHubError):
    code = "github_not_configured"


class GitHubAuthError(GitHubError):
    code = "github_auth_failed"


class GitHubUnavailable(GitHubError):
    code = "github_unavailable"


class RepositoryNotAllowed(GitHubError):
    code = "repository_not_allowed"


class RepositoryNotAccessible(GitHubError):
    code = "repository_not_accessible"


class RepositoryMoved(GitHubError):
    code = "repository_moved"


class CollaboratorsUnavailable(GitHubError):
    code = "collaborators_unavailable"


class TreeTruncated(GitHubError):
    code = "tree_truncated"


class RefNotFound(GitHubError):
    code = "ref_not_found"


class BlobUnavailable(GitHubError):
    code = "content_unavailable"


@dataclass(frozen=True, slots=True)
class Repository:
    full_name: str
    visibility: str  # private | internal | public
    default_branch: str
    archived: bool = False


@dataclass(frozen=True, slots=True)
class Collaborator:
    login: str  # lower-case
    permission: str  # highest of admin/maintain/push/triage/pull

    @property
    def can_read(self) -> bool:
        return self.permission in PERMISSION_ORDER


@dataclass(frozen=True, slots=True)
class TreeEntry:
    path: str
    mode: str
    type: str  # blob | tree | commit (submodule)
    sha: str
    size: int | None


class GitHubClient(Protocol):
    def get_repository(self, full_name: str) -> Repository: ...

    def list_collaborators(self, full_name: str) -> list[Collaborator]: ...

    def resolve_commit(self, full_name: str, ref: str) -> str: ...

    def get_tree(self, full_name: str, commit: str) -> list[TreeEntry]: ...

    def get_blob(self, full_name: str, sha: str) -> bytes: ...


GitHubClientFactory = Callable[[str], GitHubClient | None]


@dataclass(frozen=True, slots=True)
class TenantGitHubConfig:
    auth: dict[str, Any]
    allowed_repositories: frozenset[str] = field(default_factory=frozenset)
    api_base_url: str = API


def load_github_config(path: str | None) -> dict[str, TenantGitHubConfig]:
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, TenantGitHubConfig] = {}
    for slug, entry in (raw.get("tenants") or {}).items():
        auth = dict(entry.get("auth") or {})
        if auth.get("type") != "token" or not (auth.get("token") or auth.get("token_file")):
            raise ValueError(f"github connector config for {slug}: auth needs a token")
        repos = [str(r) for r in entry.get("allowed_repositories") or []]
        if bad := [r for r in repos if not REPO_RE.fullmatch(r)]:
            raise ValueError(f"github connector config for {slug}: bad repository {bad[0]}")
        out[str(slug)] = TenantGitHubConfig(
            auth=auth,
            allowed_repositories=frozenset(r.lower() for r in repos),
            api_base_url=str(entry.get("api_base_url") or API).rstrip("/"),
        )
    return out


class GitHubConnectorConfig:
    """The operator file, re-read on every call."""

    def __init__(self, path: str | None) -> None:
        self._path = path

    def tenant(self, slug: str) -> TenantGitHubConfig | None:
        return load_github_config(self._path).get(slug)

    def allowed_repositories(self, slug: str) -> frozenset[str]:
        entry = self.tenant(slug)
        return entry.allowed_repositories if entry else frozenset()


def _highest(permissions: dict[str, Any] | None, role_name: str | None) -> str | None:
    perms = permissions or {}
    for name in PERMISSION_ORDER:
        if perms.get(name):
            return name
    return {"read": "pull", "write": "push"}.get(role_name or "", role_name or None)


class HttpGitHubClient:
    def __init__(
        self,
        token: str,
        base_url: str = API,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "enterprise-ai-os-connector",
            },
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        full = url if url.startswith("http") else f"{self._base}{url}"
        try:
            return self._http.get(full, params=params)
        except httpx.HTTPError as exc:
            raise GitHubUnavailable(type(exc).__name__) from exc

    @staticmethod
    def _fail(response: httpx.Response, not_found: type[GitHubError]) -> None:
        if response.status_code == 401:
            raise GitHubAuthError("401")
        if response.status_code in (301, 302, 307, 308):
            raise RepositoryMoved(str(response.status_code))
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubUnavailable("rate_limited")
        if response.status_code in (403, 404):
            raise not_found(str(response.status_code))
        if response.status_code == 429 or response.status_code >= 500:
            raise GitHubUnavailable(str(response.status_code))
        raise GitHubError(str(response.status_code))

    def _json(self, url: str, not_found: type[GitHubError], **params: Any) -> Any:
        response = self._get(url, params or None)
        if response.status_code != 200:
            self._fail(response, not_found)
        return response.json()

    def get_repository(self, full_name: str) -> Repository:
        body = self._json(f"/repos/{full_name}", RepositoryNotAccessible)
        visibility = body.get("visibility") or ("private" if body.get("private") else "public")
        return Repository(
            full_name=str(body["full_name"]),
            visibility=str(visibility),
            default_branch=str(body.get("default_branch") or "main"),
            archived=bool(body.get("archived")),
        )

    def list_collaborators(self, full_name: str) -> list[Collaborator]:
        out: list[Collaborator] = []
        url: str | None = f"/repos/{full_name}/collaborators"
        params: dict[str, Any] | None = {"affiliation": "all", "per_page": 100}
        pages = 0
        while url:
            pages += 1
            if pages > 50:
                raise CollaboratorsUnavailable("too_many_pages")
            response = self._get(url, params)
            if response.status_code != 200:
                self._fail(response, CollaboratorsUnavailable)
            for item in response.json():
                permission = _highest(item.get("permissions"), item.get("role_name"))
                if item.get("login") and permission in PERMISSION_ORDER:
                    out.append(Collaborator(str(item["login"]).lower(), str(permission)))
            url = response.links.get("next", {}).get("url")
            params = None
        return out

    def resolve_commit(self, full_name: str, ref: str) -> str:
        if not REF_RE.fullmatch(ref):
            raise RefNotFound("invalid ref")
        body = self._json(f"/repos/{full_name}/commits/{ref}", RefNotFound)
        sha = str(body.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise RefNotFound("no sha")
        return sha

    def get_tree(self, full_name: str, commit: str) -> list[TreeEntry]:
        body = self._json(
            f"/repos/{full_name}/git/trees/{commit}", RepositoryNotAccessible, recursive=1
        )
        if body.get("truncated"):
            raise TreeTruncated("truncated")
        return [
            TreeEntry(
                path=str(e["path"]),
                mode=str(e.get("mode") or ""),
                type=str(e.get("type") or ""),
                sha=str(e.get("sha") or ""),
                size=int(e["size"]) if e.get("size") is not None else None,
            )
            for e in body.get("tree") or []
        ]

    def get_blob(self, full_name: str, sha: str) -> bytes:
        body = self._json(f"/repos/{full_name}/git/blobs/{sha}", BlobUnavailable)
        if body.get("encoding") != "base64":
            raise BlobUnavailable("encoding")
        data = base64.b64decode(body.get("content") or "")
        if len(data) > MAX_BYTES:
            raise BlobUnavailable("too_large")
        return data


def token_for(auth: dict[str, Any]) -> str:
    if auth.get("token"):
        return str(auth["token"]).strip()
    return Path(str(auth["token_file"])).read_text(encoding="utf-8").strip()


def http_client_factory(config: GitHubConnectorConfig) -> GitHubClientFactory:
    def build(tenant_slug: str) -> GitHubClient | None:
        entry = config.tenant(tenant_slug)
        if entry is None:
            return None
        return HttpGitHubClient(token_for(entry.auth), entry.api_base_url)

    return build
