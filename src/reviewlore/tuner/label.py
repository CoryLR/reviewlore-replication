"""BitsAI-CR category labeling for filtered review comments."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, get_prompt
from .dirs import DIRS
from .state import atomic_write_json, load_json, write_metadata
from reviewlore.backends.base import AgentBackend


def label_mr(
    input_path: Path,
    output_path: Path,
    config: Config,
    backend: AgentBackend,
) -> dict:
    """Label a single filtered MR file with BitsAI-CR categories.

    One LLM call per MR with all kept threads.
    """
    pr_data = load_json(input_path)
    if pr_data is None:
        return {"error": f"Could not load {input_path}"}

    threads = pr_data.get("review_threads", [])
    if not threads:
        # No threads to label, just copy
        output_data = dict(pr_data)
        atomic_write_json(output_path, output_data)
        return {"labeled": 0, "cost_usd": 0.0}

    system_prompt = get_prompt("label_bitsai", config)

    # Build user prompt with thread-starting comments
    comments_for_llm = []
    for i, thread in enumerate(threads):
        first = thread["comments"][0]
        comments_for_llm.append({
            "index": i,
            "body": first.get("body", ""),
            "file_path": first.get("file_path"),
            "line_number": first.get("line_number"),
            "is_resolved": thread.get("is_resolved"),
        })

    user_prompt = json.dumps({
        "pr_title": pr_data.get("title", ""),
        "project": pr_data.get("project", ""),
        "comments": comments_for_llm,
    }, indent=2)

    result = backend.invoke(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=config.label.model,
        budget_usd=config.label.budget,
        tools=[],  # no tools for classification
        effort_level="low",
        timeout_seconds=300,
    )

    cost = result.cost_usd

    # Apply labels
    if result.parsed_json:
        labels = result.parsed_json.get("labels", [])
        label_map = {}
        for lbl in labels:
            idx = lbl.get("index", -1)
            label_map[idx] = {
                "dimension": lbl.get("dimension", ""),
                "category": lbl.get("category", ""),
                "confidence": lbl.get("confidence", ""),
                "reasoning": lbl.get("reasoning", ""),
            }

        for i, thread in enumerate(threads):
            info = label_map.get(i, {})
            first = thread["comments"][0]
            first["dimension"] = info.get("dimension", "unknown")
            first["category"] = info.get("category", "unknown")
            first["label_confidence"] = info.get("confidence", "")
            first["label_reasoning"] = info.get("reasoning", "")
    else:
        # Parse failure: mark as unlabeled
        for thread in threads:
            first = thread["comments"][0]
            first["dimension"] = "unknown"
            first["category"] = "unknown"
            first["label_confidence"] = "parse_failure"

    output_data = dict(pr_data)
    output_data["review_threads"] = threads
    atomic_write_json(output_path, output_data)

    return {"labeled": len(threads), "cost_usd": cost}


def label_project(
    project_name: str,
    config: Config,
    backend: AgentBackend,
    out_dir: Path,
    trial: bool = False,
    force: bool = False,
) -> None:
    """Label all filtered MRs for a project with BitsAI-CR categories."""
    filtered_dir = out_dir / project_name / DIRS["filtered"]
    labeled_dir = out_dir / project_name / DIRS["labeled"]

    if not filtered_dir.exists():
        print(f"No filtered data for {project_name}. Run 'relo filter' first.")
        return

    input_files = sorted(filtered_dir.glob("pr_*.json"))
    if not input_files:
        print(f"No filtered files found in {filtered_dir}")
        return

    print(f"Labeling {project_name}: {len(input_files)} MRs")
    if trial:
        print("  TRIAL MODE")

    start = time.time()
    total_labeled = 0
    total_cost = 0.0
    processed = 0
    skipped = 0
    category_counts: dict[str, int] = {}

    for input_path in input_files:
        output_path = labeled_dir / input_path.name

        if not force and output_path.exists():
            skipped += 1
            continue

        stats = label_mr(input_path, output_path, config, backend)
        processed += 1
        total_labeled += stats.get("labeled", 0)
        total_cost += stats.get("cost_usd", 0.0)

        # Collect category distribution
        labeled_data = load_json(output_path)
        if labeled_data:
            for thread in labeled_data.get("review_threads", []):
                cat = thread["comments"][0].get("category", "unknown")
                category_counts[cat] = category_counts.get(cat, 0) + 1

        if processed % 25 == 0:
            print(f"  Processed {processed}/{len(input_files) - skipped}...")

    elapsed = time.time() - start
    total_files = len(list(labeled_dir.glob("pr_*.json")))

    print(f"  Labeled: {total_files} MRs ({processed} new, {skipped} skipped)")
    print(f"  Total comments labeled: {total_labeled}")
    if category_counts:
        top = sorted(category_counts.items(), key=lambda x: -x[1])[:5]
        print(f"  Top categories: {', '.join(f'{c}({n})' for c, n in top)}")
    print(f"  Cost: ${total_cost:.2f}")

    if processed == 0 and (labeled_dir / "metadata.json").exists():
        # Skip-existing replay run: preserve historical cost + provenance.
        return

    write_metadata(labeled_dir, {
        "step": "label",
        "project": project_name,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "params": {"trial": trial},
        "prompt_versions": {
            "label_bitsai": config.prompts["label_bitsai"].version,
        },
        "outputs": {
            "total_mrs": total_files,
            "total_labeled_comments": total_labeled,
            "category_distribution": category_counts,
        },
        "cost": {"total_usd": total_cost},
        "duration_seconds": elapsed,
    })
