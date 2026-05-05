"""Count learned rules per BitsAI-CR dimension per project per volume.

Parses the tuned-prompt snapshots produced by the consolidator at
``data/<project>/5_optimization/snapshots/tuned_<N>.md`` and emits per-dimension
rule counts.  The snapshot format is the 2-tier markdown written by
``reviewlore.tuner.optimize.render_rules_markdown``:

    - **Dimension Name**
      - rule text
      - another rule
    - **Next Dimension**
      - ...

A "rule" is any line that begins with ``  - `` (two spaces, a hyphen, a
space) under the most recently seen dimension header.  Dimension headers
match the BitsAI-CR four: Code Defect, Maintainability and Readability,
Performance Issue, Code Style.

Usage::

    python3 scripts/rule_counts.py
    python3 scripts/rule_counts.py --projects storybook,mermaid,ase --volumes 100,200
    python3 scripts/rule_counts.py --json out/rule_counts.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


DIMENSIONS = [
    "Code Defect",
    "Maintainability and Readability",
    "Performance Issue",
    "Code Style",
]

_HEADER_RE = re.compile(r"^- \*\*(.+?)\*\*\s*$")
_RULE_RE = re.compile(r"^  - ")


def count_rules(snapshot_text: str) -> dict[str, int]:
    """Return a dict mapping each of the four dimensions to a rule count.

    Unknown or missing dimensions map to zero.  Rules under non-standard
    dimension headers are counted into an ``"other"`` bucket that the
    caller can inspect or ignore.
    """
    counts: dict[str, int] = {d: 0 for d in DIMENSIONS}
    counts["other"] = 0
    current: str | None = None
    for line in snapshot_text.splitlines():
        if line.startswith("- "):
            m = _HEADER_RE.match(line)
            if m:
                current = m.group(1).strip()
                if current not in counts:
                    # Track an unknown dimension explicitly rather than silently dropping.
                    counts.setdefault(current, 0)
                continue
        if _RULE_RE.match(line):
            if current is None:
                counts["other"] += 1
            elif current in counts:
                counts[current] += 1
            else:
                counts[current] = counts.get(current, 0) + 1
    return counts


def snapshot_path(data_dir: Path, project: str, volume: int | str) -> Path:
    return data_dir / project / "5_optimization" / "snapshots" / f"tuned_{volume}.md"


def discover_projects(data_dir: Path) -> list[str]:
    return sorted(
        p.name for p in data_dir.iterdir()
        if p.is_dir() and (p / "5_optimization" / "snapshots").is_dir()
    )


def discover_volumes(data_dir: Path, projects: list[str]) -> list[int]:
    volumes: set[int] = set()
    for proj in projects:
        snap_dir = data_dir / proj / "5_optimization" / "snapshots"
        if not snap_dir.is_dir():
            continue
        for child in snap_dir.glob("tuned_*.md"):
            m = re.match(r"tuned_(\d+)\.md$", child.name)
            if m:
                volumes.add(int(m.group(1)))
    return sorted(volumes)


def compute(
    data_dir: Path,
    projects: list[str],
    volumes: list[int],
) -> dict:
    result: dict = {"data_dir": str(data_dir), "projects": {}}
    for proj in projects:
        per_volume: dict[str, dict] = {}
        for v in volumes:
            path = snapshot_path(data_dir, proj, v)
            if not path.is_file():
                per_volume[str(v)] = {"present": False, "path": str(path)}
                continue
            counts = count_rules(path.read_text())
            total = sum(c for d, c in counts.items() if d != "other") + counts.get("other", 0)
            per_volume[str(v)] = {
                "present": True,
                "path": str(path),
                "counts": counts,
                "total": total,
            }
        result["projects"][proj] = per_volume
    return result


def render_markdown(result: dict) -> str:
    lines: list[str] = []
    lines.append("# Learned-rule counts by dimension")
    lines.append("")
    lines.append(f"- Data directory: `{result['data_dir']}`")
    lines.append("")
    for proj, volumes in result["projects"].items():
        lines.append(f"## {proj}")
        lines.append("")
        for volume_label, data in volumes.items():
            if not data.get("present"):
                lines.append(f"- tuned_{volume_label}: *missing* (`{data['path']}`)")
                continue
            counts = data["counts"]
            total = data["total"]
            pieces = [f"{d}={counts.get(d, 0)}" for d in DIMENSIONS]
            if counts.get("other", 0):
                pieces.append(f"other={counts['other']}")
            # Surface unknown dimensions if the consolidator ever emits them.
            extras = [
                f"{d}={c}" for d, c in counts.items()
                if d not in DIMENSIONS and d != "other" and c > 0
            ]
            pieces.extend(extras)
            lines.append(f"- tuned_{volume_label} ({total} rules): {', '.join(pieces)}")
        lines.append("")
    return "\n".join(lines)


def parse_list(s: str | None) -> list[str] | None:
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def parse_int_list(s: str | None) -> list[int] | None:
    if not s:
        return None
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--projects", help="Comma-separated project names (default: auto-discover)")
    parser.add_argument("--volumes", help="Comma-separated tuning volumes (default: auto-discover)")
    parser.add_argument("--json", help="Optional JSON output path")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        print(f"ERROR: data dir not found: {data_dir}", file=sys.stderr)
        return 1

    projects = parse_list(args.projects) or discover_projects(data_dir)
    if not projects:
        print("ERROR: no projects discovered", file=sys.stderr)
        return 1
    volumes = parse_int_list(args.volumes) or discover_volumes(data_dir, projects)
    if not volumes:
        print("ERROR: no tuning volumes discovered", file=sys.stderr)
        return 1

    result = compute(data_dir, projects, volumes)
    print(render_markdown(result))

    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
