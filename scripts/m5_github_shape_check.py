#!/usr/bin/env python3
"""Read-only live check that the GitHub connector parses real GitHub REST responses.

    backend/.venv/bin/python scripts/m5_github_shape_check.py --repo <owner/name>

The operator's own ``gh`` CLI (already signed in) fetches the real responses for one
repository: repository, collaborators (``affiliation=all``), the branch head commit,
the recursive tree and one blob. They are replayed, byte for byte, to
``app.connectors.github.HttpGitHubClient`` through an httpx mock transport, so the
connector's parsing is exercised on live data **without** handing the operator's
personal token to application code. Nothing is written to GitHub.

This is not the live acceptance run (``scripts/m5_github_acceptance.py``): it does not
sync, does not test revocation and uses no dedicated test repository. The evidence
records counts and field presence only (no logins, emails or file contents).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402

from app.connectors.github import HttpGitHubClient  # noqa: E402


def gh(path: str) -> tuple[int, Any, dict[str, str]]:
    """GET through the gh CLI; returns (status, json body, headers)."""

    proc = subprocess.run(  # noqa: S603 - fixed argv, read-only GET
        ["gh", "api", "-i", "-H", "X-GitHub-Api-Version: 2022-11-28", path],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    head, _, body = proc.stdout.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    status = int(lines[0].split()[1]) if lines and lines[0].startswith("HTTP") else 0
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return status, json.loads(body) if body.strip() else None, headers


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    repo = args.repo
    recorded: dict[str, tuple[int, Any]] = {}

    status, body, _ = gh(f"/repos/{repo}")
    recorded[f"/repos/{repo}"] = (status, body)
    branch = body["default_branch"]
    status, collaborators, _ = gh(f"/repos/{repo}/collaborators?affiliation=all&per_page=100")
    recorded[f"/repos/{repo}/collaborators"] = (status, collaborators)
    status, commit, _ = gh(f"/repos/{repo}/commits/{branch}")
    recorded[f"/repos/{repo}/commits/{branch}"] = (status, commit)
    sha = commit["sha"]
    status, tree, _ = gh(f"/repos/{repo}/git/trees/{sha}?recursive=1")
    recorded[f"/repos/{repo}/git/trees/{sha}"] = (status, tree)
    blob_entry = next(e for e in tree["tree"] if e["type"] == "blob" and e.get("size", 0) < 20000)
    status, blob, _ = gh(f"/repos/{repo}/git/blobs/{blob_entry['sha']}")
    recorded[f"/repos/{repo}/git/blobs/{blob_entry['sha']}"] = (status, blob)

    def replay(request: httpx.Request) -> httpx.Response:
        code, payload = recorded[request.url.path]
        return httpx.Response(code, json=payload)

    client = HttpGitHubClient("not-a-token", transport=httpx.MockTransport(replay))
    parsed_repo = client.get_repository(repo)
    parsed_collaborators = client.list_collaborators(repo)
    parsed_commit = client.resolve_commit(repo, branch)
    parsed_tree = client.get_tree(repo, parsed_commit)
    data = client.get_blob(repo, blob_entry["sha"])
    import hashlib

    git_id = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()  # noqa: S324 - git id
    evidence = {
        "kind": "m5_github_shape_check",
        "live_responses": True,
        "writes_to_github": False,
        "sync_or_revocation_tested": False,
        "checked_at": datetime.now(UTC).isoformat(),
        "repository_visibility": parsed_repo.visibility,
        "default_branch_resolved": parsed_commit == sha,
        "collaborators": {
            "http": recorded[f"/repos/{repo}/collaborators"][0],
            "count": len(parsed_collaborators),
            "permission_levels": sorted({c.permission for c in parsed_collaborators}),
            "fields_used_present": all(
                "login" in c and ("permissions" in c or "role_name" in c) for c in collaborators
            ),
        },
        "tree": {
            "truncated": tree.get("truncated"),
            "entries": len(parsed_tree),
            "blobs": sum(1 for e in parsed_tree if e.type == "blob"),
            "symlinks": sum(1 for e in parsed_tree if e.mode == "120000"),
            "submodules": sum(1 for e in parsed_tree if e.type == "commit"),
        },
        "blob_decoded_and_git_id_matches": git_id == blob_entry["sha"],
    }
    text = json.dumps(evidence, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
    ok = evidence["default_branch_resolved"] and evidence["blob_decoded_and_git_id_matches"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
