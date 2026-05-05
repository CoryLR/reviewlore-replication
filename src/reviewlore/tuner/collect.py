"""MR/PR data collection from GitHub GraphQL and GitLab REST APIs."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .config import Config, ProjectConfig
from .dirs import DIRS
from .state import atomic_write_json, write_metadata

# Load .env from the config directory (repo root)
load_dotenv()


def _parse_cli_date(s: str) -> datetime:
    """Parse an ISO 8601 date/datetime from a CLI argument as UTC."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_mr_date(s: str | None) -> datetime | None:
    """Parse a merged_at timestamp. Returns None if missing/unparseable."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

# ---------------------------------------------------------------------------
# GitHub GraphQL
# ---------------------------------------------------------------------------

QUERY_LIST_PRS = """
query($owner: String!, $name: String!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      states: MERGED
      first: $first
      after: $after
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        mergedAt
        baseRefName
        mergeCommit {
          oid
          parents(first: 3) { totalCount }
        }
        reviewThreads { totalCount }
      }
    }
  }
  rateLimit { remaining resetAt cost }
}
"""

QUERY_PR_DETAILS = """
query($owner: String!, $name: String!, $number: Int!, $threadCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      title
      body
      author { login }
      mergedAt
      baseRefName
      mergeCommit {
        oid
        parents(first: 3) {
          totalCount
          nodes { oid }
        }
      }
      reviewThreads(first: 50, after: $threadCursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          isOutdated
          isCollapsed
          comments(first: 20) {
            nodes {
              id
              body
              author { login }
              path
              line
              originalLine
              startLine
              outdated
              createdAt
              commit { oid }
            }
          }
        }
      }
    }
  }
  rateLimit { remaining resetAt cost }
}
"""


def _github_graphql(
    query: str,
    variables: dict | None,
    token: str,
    delay: float,
) -> dict:
    """Execute a GitHub GraphQL query with rate limiting."""
    time.sleep(delay)
    resp = httpx.post(
        "https://api.github.com/graphql",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        msgs = "; ".join(e.get("message", "?") for e in data["errors"])
        raise RuntimeError(f"GraphQL errors: {msgs}")
    return data["data"]


def _github_fetch_pr_list(
    owner: str,
    name: str,
    token: str,
    config: Config,
    max_prs: int,
    merged_before: datetime | None = None,
) -> list[dict]:
    """Fetch paginated list of merged PRs from GitHub, filtering for quality."""
    delay = config.collection.rate_limit_delay
    min_threads = config.collection.min_review_threads
    all_prs: list[dict] = []
    skipped_post_cutoff = 0
    cursor = None
    page = 0

    while True:
        page += 1
        _log(f"Fetching PR list page {page}...")

        data = _github_graphql(QUERY_LIST_PRS, {
            "owner": owner, "name": name,
            "first": 50, "after": cursor,
        }, token, delay)

        pr_data = data["repository"]["pullRequests"]
        prs = pr_data["nodes"]
        page_info = pr_data["pageInfo"]

        for pr in prs:
            mc = pr.get("mergeCommit")
            thread_count = pr["reviewThreads"]["totalCount"]

            if mc and mc["parents"]["totalCount"] < 2:
                continue
            if thread_count < min_threads:
                continue
            if merged_before is not None:
                merged_dt = _parse_mr_date(pr.get("mergedAt"))
                if merged_dt is None or merged_dt > merged_before:
                    skipped_post_cutoff += 1
                    continue

            all_prs.append(pr)
            if len(all_prs) >= max_prs:
                _log(f"Reached target ({max_prs} PRs)")
                return all_prs

        rate = data.get("rateLimit", {})
        remaining = rate.get("remaining", "?")
        cutoff_note = f", {skipped_post_cutoff} post-cutoff" if merged_before else ""
        _log(f"  Page {page}: {len(prs)} scanned, {len(all_prs)} qualifying{cutoff_note} (rate limit: {remaining} remaining)")

        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    return all_prs


def _normalize_github_comment(c: dict) -> dict:
    """Project a GitHub GraphQL ``PullRequestReviewComment`` node into our schema.

    Notes:
        ``line_number`` falls back from ``line`` to ``originalLine`` because
        GitHub returns ``line: null`` for review comments on outdated threads
        (where the anchor line moved or was deleted). The original anchor is
        preserved in ``originalLine``. Without the fallback, the judge prompt
        loses line numbers for all outdated-thread comments.
    """
    return {
        "id": c["id"],
        "author": (c.get("author") or {}).get("login", "ghost"),
        "body": c["body"],
        "file_path": c.get("path"),
        "line_number": c.get("line") or c.get("originalLine"),
        "start_line": c.get("startLine"),
        "commit_sha": (c.get("commit") or {}).get("oid"),
        "created_at": c["createdAt"],
        "is_outdated": c.get("outdated", False),
    }


def _github_fetch_pr_details(
    owner: str,
    name: str,
    pr_number: int,
    token: str,
    delay: float,
) -> dict:
    """Fetch full PR details including paginated review threads."""
    all_threads: list[dict] = []
    cursor = None
    pr_metadata = None

    while True:
        data = _github_graphql(QUERY_PR_DETAILS, {
            "owner": owner, "name": name,
            "number": pr_number, "threadCursor": cursor,
        }, token, delay)

        pr = data["repository"]["pullRequest"]
        if pr_metadata is None:
            pr_metadata = {
                "platform": "github",
                "project": f"{owner}/{name}",
                "number": pr["number"],
                "title": pr["title"],
                "body": pr["body"],
                "author": (pr.get("author") or {}).get("login", "ghost"),
                "merged_at": pr["mergedAt"],
                "base_ref": pr.get("baseRefName", ""),
                "merge_commit_sha": pr["mergeCommit"]["oid"],
                "merge_commit_parent_count": pr["mergeCommit"]["parents"]["totalCount"],
                "merge_commit_parent_shas": [
                    p["oid"] for p in pr["mergeCommit"]["parents"]["nodes"]
                ],
            }

        threads_data = pr["reviewThreads"]
        all_threads.extend(threads_data["nodes"])

        if not threads_data["pageInfo"]["hasNextPage"]:
            break
        cursor = threads_data["pageInfo"]["endCursor"]

    # Normalize threads
    normalized = []
    for thread in all_threads:
        comments = [_normalize_github_comment(c) for c in thread["comments"]["nodes"]]
        normalized.append({
            "is_resolved": thread["isResolved"],
            "is_outdated": thread["isOutdated"],
            "is_collapsed": thread.get("isCollapsed", False),
            "comments": comments,
        })

    pr_metadata["review_threads"] = normalized
    pr_metadata["review_thread_count"] = len(normalized)
    return pr_metadata


def _collect_github(
    project: ProjectConfig,
    config: Config,
    out_dir: Path,
    max_prs: int,
    merged_before: datetime | None = None,
) -> dict:
    """Collect PR data from GitHub."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set in environment")

    owner, name = project.repo.split("/")
    delay = config.collection.rate_limit_delay
    data_dir = out_dir / DIRS["collected"]

    _log(f"Collecting from GitHub: {project.repo}")
    _log(f"Target: {max_prs} PRs, min {config.collection.min_review_threads} review threads")
    if merged_before is not None:
        _log(f"Merged-before cutoff: {merged_before.isoformat()}")

    # Step 1: Get filtered PR list
    pr_list = _github_fetch_pr_list(owner, name, token, config, max_prs, merged_before)
    _log(f"Found {len(pr_list)} qualifying PRs")

    # Step 2: Fetch details
    collected = 0
    skipped = 0
    total_cost = 0.0

    for i, pr_summary in enumerate(pr_list):
        pr_number = pr_summary["number"]
        pr_file = data_dir / f"pr_{pr_number}.json"

        if pr_file.exists():
            skipped += 1
            continue

        _log(f"  [{i + 1}/{len(pr_list)}] PR #{pr_number}: {pr_summary.get('title', '?')[:60]}")

        try:
            details = _github_fetch_pr_details(owner, name, pr_number, token, delay)
            atomic_write_json(pr_file, details)
            _log(f"    Saved ({details['review_thread_count']} threads)")
            collected += 1
        except Exception as e:
            _log(f"    ERROR: {e}")
            continue

    _log(f"Done. Collected {collected}, skipped {skipped} existing")
    return {"collected": collected, "skipped": skipped, "total": len(pr_list)}


# ---------------------------------------------------------------------------
# GitLab REST
# ---------------------------------------------------------------------------

def _gitlab_api_get(
    path: str,
    params: dict | None,
    token: str,
    delay: float,
    instance: str = "https://gitlab.com",
) -> httpx.Response:
    """GET request to GitLab API with rate limiting."""
    time.sleep(delay)
    url = f"{instance}/api/v4{path}"
    headers = {"PRIVATE-TOKEN": token} if token else {}
    return httpx.get(url, headers=headers, params=params or {}, timeout=30)


def _gitlab_fetch_mr_list(
    project_id: int,
    token: str,
    config: Config,
    max_mrs: int,
    instance: str = "https://gitlab.com",
    merged_before: datetime | None = None,
) -> list[dict]:
    """Fetch paginated list of merged MRs from GitLab."""
    delay = config.collection.rate_limit_delay
    all_mrs: list[dict] = []
    skipped_post_cutoff = 0
    page = 0

    while True:
        page += 1
        _log(f"Fetching MR list page {page}...")

        resp = _gitlab_api_get(
            f"/projects/{project_id}/merge_requests",
            {"state": "merged", "order_by": "updated_at", "sort": "desc",
             "per_page": 100, "page": page},
            token, delay, instance,
        )
        if resp.status_code != 200:
            _log(f"MR list failed: HTTP {resp.status_code}")
            break

        mrs = resp.json()
        if not mrs:
            break

        for mr in mrs:
            if bool(mr.get("squash_commit_sha")):
                continue
            if merged_before is not None:
                merged_dt = _parse_mr_date(mr.get("merged_at"))
                if merged_dt is None or merged_dt > merged_before:
                    skipped_post_cutoff += 1
                    continue
            all_mrs.append(mr)
            if len(all_mrs) >= max_mrs:
                _log(f"Reached target ({max_mrs} MRs)")
                return all_mrs

        cutoff_note = f", {skipped_post_cutoff} post-cutoff" if merged_before else ""
        _log(f"  Page {page}: {len(mrs)} fetched, {len(all_mrs)} qualifying{cutoff_note}")

        if len(mrs) < 100:
            break

    return all_mrs


def _gitlab_fetch_discussions(
    project_id: int,
    mr_iid: int,
    token: str,
    delay: float,
    instance: str = "https://gitlab.com",
) -> list[dict]:
    """Fetch all discussion threads for a GitLab MR."""
    all_discussions: list[dict] = []
    page = 0

    while True:
        page += 1
        resp = _gitlab_api_get(
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions",
            {"per_page": 100, "page": page},
            token, delay, instance,
        )
        if resp.status_code != 200:
            break

        discussions = resp.json()
        if not discussions:
            break

        all_discussions.extend(discussions)
        if len(discussions) < 100:
            break

    return all_discussions


def _normalize_gitlab_discussions(discussions: list[dict]) -> list[dict]:
    """Normalize GitLab discussions into shared thread format."""
    threads = []
    for disc in discussions:
        notes = disc.get("notes", [])
        if not notes:
            continue

        first_note = notes[0]
        if first_note.get("system", False):
            continue

        is_inline = (
            first_note.get("type") == "DiffNote"
            and first_note.get("position") is not None
        )
        is_resolved = first_note.get("resolved", False)

        comments = []
        for note in notes:
            if note.get("system", False):
                continue
            pos = note.get("position", {}) or {}
            comments.append({
                "id": str(note.get("id", "")),
                "author": note.get("author", {}).get("username", "unknown"),
                "body": note.get("body", ""),
                "file_path": pos.get("new_path") if is_inline else None,
                "line_number": pos.get("new_line") if is_inline else None,
                "commit_sha": pos.get("head_sha") if is_inline else None,
                "base_sha": pos.get("base_sha") if is_inline else None,
                "created_at": note.get("created_at", ""),
                "is_outdated": False,
            })

        threads.append({
            "is_resolved": is_resolved,
            "is_outdated": False,
            "is_inline": is_inline,
            "comments": comments,
        })

    return threads


def _collect_gitlab(
    project: ProjectConfig,
    config: Config,
    out_dir: Path,
    max_mrs: int,
    merged_before: datetime | None = None,
) -> dict:
    """Collect MR data from GitLab."""
    token = os.environ.get("GITLAB_TOKEN", "")
    delay = config.collection.rate_limit_delay
    instance = "https://gitlab.com"
    data_dir = out_dir / DIRS["collected"]

    _log(f"Collecting from GitLab: {project.repo}")
    if merged_before is not None:
        _log(f"Merged-before cutoff: {merged_before.isoformat()}")

    # Look up project ID
    encoded = urllib.parse.quote(project.repo, safe="")
    resp = _gitlab_api_get(f"/projects/{encoded}", None, token, delay, instance)
    if resp.status_code != 200:
        raise RuntimeError(f"GitLab project lookup failed: HTTP {resp.status_code}")
    project_info = resp.json()
    project_id = project_info["id"]
    _log(f"Project ID: {project_id}")

    # Fetch MR list
    mr_list = _gitlab_fetch_mr_list(project_id, token, config, max_mrs, instance, merged_before)
    _log(f"Found {len(mr_list)} qualifying MRs")

    collected = 0
    skipped = 0

    for i, mr in enumerate(mr_list):
        iid = mr["iid"]
        mr_file = data_dir / f"pr_{iid}.json"

        if mr_file.exists():
            skipped += 1
            continue

        _log(f"  [{i + 1}/{len(mr_list)}] MR !{iid}: {mr['title'][:60]}")

        try:
            discussions = _gitlab_fetch_discussions(
                project_id, iid, token, delay, instance
            )
            threads = _normalize_gitlab_discussions(discussions)

            if not threads:
                continue

            record = {
                "platform": "gitlab",
                "project": project.repo,
                "number": iid,
                "title": mr["title"],
                "body": mr.get("description", ""),
                "author": mr.get("author", {}).get("username", "unknown"),
                "merged_at": mr.get("merged_at"),
                "base_ref": mr.get("target_branch", ""),
                "merge_commit_sha": mr.get("merge_commit_sha"),
                "merge_commit_parent_count": 2,  # non-squash merges are 2-parent
                "review_threads": threads,
                "review_thread_count": len(threads),
            }

            atomic_write_json(mr_file, record)
            _log(f"    Saved ({len(threads)} threads)")
            collected += 1
        except Exception as e:
            _log(f"    ERROR: {e}")
            continue

    _log(f"Done. Collected {collected}, skipped {skipped} existing")
    return {"collected": collected, "skipped": skipped, "total": len(mr_list)}


# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------

_start_time = 0.0


def _log(msg: str) -> None:
    elapsed = time.time() - _start_time
    minutes, seconds = divmod(int(elapsed), 60)
    print(f"  [{minutes:02d}:{seconds:02d}] {msg}", flush=True)


def collect(
    project_name: str,
    config: Config,
    out_dir: Path,
    trial: bool = False,
    force: bool = False,
    merged_before: str | None = None,
) -> None:
    """Collect MR/PR data for a project.

    Dispatches to GitHub or GitLab based on project config.
    Resume: skips MRs with existing output files.
    If merged_before is given, only MRs with merged_at on or before the cutoff
    are collected; API pagination continues past post-cutoff MRs since the API
    sort order is updated_at (not merged_at).
    """
    global _start_time
    _start_time = time.time()

    project = config.projects[project_name]
    proj_out = out_dir / project_name
    data_dir = proj_out / DIRS["collected"]

    max_mrs = 10 if trial else config.collection.target_mrs
    merged_before_dt = _parse_cli_date(merged_before) if merged_before else None

    # Check for existing data
    if not force:
        existing = list(data_dir.glob("pr_*.json")) if data_dir.exists() else []
        if len(existing) >= max_mrs:
            _log(f"Already have {len(existing)} MRs (target: {max_mrs}). Use --force to re-collect.")
            return

    print(f"Collecting {project_name} ({project.platform}: {project.repo})")
    print(f"  Target: {max_mrs} MRs, output: {proj_out}")
    if merged_before_dt is not None:
        print(f"  Merged-before: {merged_before_dt.isoformat()}")
    if trial:
        print(f"  TRIAL MODE")
    print()

    if project.platform == "github":
        stats = _collect_github(project, config, proj_out, max_mrs, merged_before_dt)
    elif project.platform == "gitlab":
        stats = _collect_gitlab(project, config, proj_out, max_mrs, merged_before_dt)
    else:
        raise ValueError(f"Unknown platform: {project.platform}")

    # Write metadata
    existing_count = len(list(data_dir.glob("pr_*.json")))
    metadata_params = {
        "target_mrs": max_mrs,
        "min_review_threads": config.collection.min_review_threads,
        "trial": trial,
    }
    if merged_before_dt is not None:
        metadata_params["merged_before"] = merged_before_dt.isoformat()
    write_metadata(data_dir, {
        "step": "collect",
        "project": project_name,
        "platform": project.platform,
        "repo": project.repo,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "params": metadata_params,
        "outputs": {
            "total_mrs": existing_count,
        },
    })

    elapsed = time.time() - _start_time
    print(f"\nCollection complete: {existing_count} MRs in {elapsed:.0f}s")
