#!/usr/bin/env bash
# Reconstruct stripped worktree/ directories under
# data/judge_validation/samples/sample_*/ from the bundled bare subject-repos.
#
# The replication artifact ships subject-repos/<project>/ as shallow bare
# clones containing each sample's head_sha + merge_base_sha trees, but it
# strips the per-sample worktree/ directories to keep the archive small.
# This helper provisions worktree/ from the bundled bare repo on demand,
# matching the layout the live validation workflow expects.
#
# Usage (from the artifact root):
#   bash scripts/reconstruct_sample_worktrees.sh
#
# Idempotent: skips samples whose worktree/ already exists.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Prune stale worktree registrations from each bundled bare subject-repo
# before adding fresh ones. The artifact rebuild flow may have left dangling
# registrations behind if a prior reconstruction's worktree dirs were moved
# aside via replace_tree.
for bare in "$ROOT"/subject-repos/*/; do
    [ -d "$bare" ] || continue
    git -C "$bare" worktree prune 2>/dev/null || true
done

provisioned=0
skipped=0
for info in "$ROOT"/data/judge_validation/samples/sample_*/sample_info.json; do
    [ -f "$info" ] || continue
    sample_dir="$(dirname "$info")"
    worktree_path="$sample_dir/worktree"
    if [ -d "$worktree_path" ]; then
        skipped=$((skipped + 1))
        continue
    fi
    project="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project"])' "$info")"
    head_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["head_sha"])' "$info")"
    bare="$ROOT/subject-repos/$project"
    if [ ! -d "$bare" ]; then
        echo "WARN: missing subject-repo $bare; cannot provision $worktree_path" >&2
        continue
    fi
    echo "provisioning: ${worktree_path#$ROOT/} -> $project@$head_sha"
    git -C "$bare" worktree add --detach "$worktree_path" "$head_sha"
    # Restore the `worktree` folder entry in the VS Code workspace file so
    # opening it shows the sample folder and the worktree side-by-side
    # (build_artifact.py strips the entry pre-reconstruction so opening the
    # workspace before this script runs does not flash a missing-folder
    # warning).
    workspace_file="$sample_dir/workspace.code-workspace"
    if [ -f "$workspace_file" ]; then
        python3 - "$workspace_file" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    ws = json.load(f)
folders = ws.get("folders") or []
if not any(isinstance(f, dict) and f.get("path") == "worktree" for f in folders):
    folders.append({"path": "worktree"})
    ws["folders"] = folders
    with open(path, "w") as f:
        json.dump(ws, f, indent=2)
        f.write("\n")
PY
    fi
    provisioned=$((provisioned + 1))
done

echo "done: provisioned $provisioned, skipped $skipped (already present)"
