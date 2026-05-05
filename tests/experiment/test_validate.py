"""Unit tests for the verdict-level case-cohort validate.py implementation.

Covers seed determinism, verdict-pool shuffle stability, stratified verdict-
level sampling, census fallback, extension target-raise (sample-key prefix
invariance, no relabeling), drift guards, worktree porcelain matrix, compare
pooled+per-stratum+kappa+mid-labeling+labeling-integrity, status counts,
atomic manifest write on abort, CLI smoke test.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from reviewlore.experiment import validate as vmod
from reviewlore.experiment.cli import cli as relox_cli


# ---------------------------------------------------------------------------
# 1. Seed determinism
# ---------------------------------------------------------------------------


def test_per_cell_seed_is_deterministic():
    """sha256-derived seeds must not vary across processes or hash salts."""
    s1 = vmod._per_cell_seed(42, "storybook", "match")
    s2 = vmod._per_cell_seed(42, "storybook", "match")
    assert s1 == s2
    ref = int.from_bytes(
        hashlib.sha256("42|storybook|match".encode("utf-8")).digest()[:8], "big"
    )
    assert s1 == ref
    # Different stratum suffixes yield different seeds
    assert vmod._per_cell_seed(42, "storybook", "no_match") != s1
    assert vmod._per_cell_seed(42, "mermaid", "match") != s1


def test_shuffled_pool_is_seed_stable():
    """Stable-sort + seeded-shuffle must be deterministic across invocations."""
    pool = [
        vmod.VerdictLocator(
            project="p", condition="c", pr_dir=f"pr_{i}", rev_dir=f"rev_{i:08x}",
            pr_number=i, head_sha="0" * 40, merge_base_sha="0" * 40,
            human_comment_index=0, judge_verdict="match",
            human_file=None, human_line=None, human_summary="",
            matched_ai_comment_index=None, reasoning="",
        )
        for i in range(20)
    ]
    a = vmod._shuffled_pool(42, "p", "match", pool)
    b = vmod._shuffled_pool(42, "p", "match", pool)
    assert [v.sample_key for v in a] == [v.sample_key for v in b]
    c = vmod._shuffled_pool(43, "p", "match", pool)
    assert [v.sample_key for v in c] != [v.sample_key for v in a]


# ---------------------------------------------------------------------------
# 2. Blank verdicts.json construction (rater-facing)
# ---------------------------------------------------------------------------


def test_blank_verdicts_drops_runtime_fields_and_nulls_decisions():
    judge = {
        "verdicts": [
            {
                "human_comment_index": 0, "human_file": "src/x.py", "human_line": 10,
                "human_summary": "concern A", "verdict": "match",
                "matched_ai_comment_index": 2, "reasoning": "judge's reasoning",
            },
            {
                "human_comment_index": 1, "human_file": "src/y.py", "human_line": None,
                "human_summary": "concern B", "verdict": "no_match",
                "matched_ai_comment_index": None, "reasoning": "also judge",
            },
        ],
        # Stale top-level summary block: must be dropped, not echoed.
        "summary": {"total_human_comments": 2, "matches": 1, "no_matches": 1, "recall": 0.5, "unmatched_ai_comments": 0},
        "cost_usd": 0.22, "duration_seconds": 60.0,
    }
    blank = vmod._build_blank_verdicts(judge)
    assert "cost_usd" not in blank
    assert "duration_seconds" not in blank
    assert "summary" not in blank, "top-level summary block was removed in 2026-05-04 judge-fix-2"
    assert set(blank.keys()) == {"verdicts"}
    assert len(blank["verdicts"]) == 2
    for i, entry in enumerate(blank["verdicts"]):
        assert entry["human_comment_index"] == i
        assert entry["human_file"] == judge["verdicts"][i]["human_file"]
        assert entry["human_line"] == judge["verdicts"][i]["human_line"]
        assert entry["human_summary"] == judge["verdicts"][i]["human_summary"]
        assert entry["verdict"] is None
        assert entry["matched_ai_comment_index"] is None
        assert entry["reasoning"] is None


# ---------------------------------------------------------------------------
# 3. Worktree provisioning recipe (porcelain matrix)
# ---------------------------------------------------------------------------


def test_worktree_porcelain_matrix_M_untracked_D(synthetic_subject_repo: Path, tmp_path: Path):
    shas = json.loads((synthetic_subject_repo.parent / f"{synthetic_subject_repo.name}.shas.json").read_text())
    worktree = tmp_path / "wt"
    vmod._provision_worktree(synthetic_subject_repo, worktree, shas["head"], shas["base"])

    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(worktree), capture_output=True, text=True, check=True
    ).stdout
    lines = set(porcelain.splitlines())
    assert any(line.endswith("keep.txt") and line.startswith(" M") for line in lines), porcelain
    assert any(line.endswith("new.txt") and line.startswith("??") for line in lines), porcelain
    assert any(line.endswith("remove.txt") and line.startswith(" D") for line in lines), porcelain

    assert (worktree / "keep.txt").read_text() == "head\n"
    assert (worktree / "new.txt").read_text() == "added by PR\n"
    assert not (worktree / "remove.txt").exists()

    subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=str(synthetic_subject_repo), check=True)


# ---------------------------------------------------------------------------
# 4. Stratified verdict-level sampling (end-to-end setup)
# ---------------------------------------------------------------------------


def test_setup_six_cells_hit_floor(synthetic_data_root):
    """3 projects x {match, no_match} = 6 strata; target=12 -> floor=2/stratum."""
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)
    manifest = json.loads((data_root / "judge_validation" / "manifest.json").read_text())
    assert manifest["pooled_target"] == 12
    assert manifest["derived_per_cell_target_at_latest_setup"] == 2  # ceil(12/6) = 2
    assert manifest["scheme"] == "verdict_level_case_cohort"

    # Expect all 6 strata present
    assert set(manifest["strata"].keys()) == {
        "proj_a/match", "proj_a/no_match",
        "proj_b/match", "proj_b/no_match",
        "proj_c/match", "proj_c/no_match",
    }
    for k, st in manifest["strata"].items():
        assert st["per_cell_target"] == 2
        assert len(st["sample_keys"]) == 2, f"stratum {k} expected 2 sample keys, got {len(st['sample_keys'])}"

    # Every project should have its prompt-version-at-run recorded
    for proj in config.projects:
        assert manifest["projects"][proj]["judge_prompt_version_at_run"] == 2


def test_setup_creates_sample_folders_with_required_files(synthetic_data_root):
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)
    samples = data_root / "judge_validation" / "samples"
    assert samples.exists()
    sample_dirs = sorted(samples.iterdir())
    assert len(sample_dirs) >= 6  # at least one per stratum; possibly more due to rev-level grouping

    for sd in sample_dirs:
        for name in ["README.md", "sample_info.json", "context.json", "review.json",
                     "verdicts.json", "judge_system_prompt.md", "workspace.code-workspace"]:
            assert (sd / name).exists(), f"missing {name} in {sd}"
        blank = json.loads((sd / "verdicts.json").read_text())
        for v in blank["verdicts"]:
            assert v["verdict"] is None
        assert (sd / "worktree").exists()


def test_setup_manifest_verdict_entries_are_clean(synthetic_data_root):
    """Manifest verdict entries carry only the inclusion-driving sample_key
    plus judge bookkeeping; no per-verdict tagging fields leak in."""
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)
    manifest = json.loads((data_root / "judge_validation" / "manifest.json").read_text())

    expected_keys = {
        "sample_key", "human_comment_index", "human_file", "human_line",
        "human_summary", "judge_verdict_at_sampling",
        "matched_ai_comment_index_at_sampling", "reasoning_at_sampling",
        "reviewer_output_path", "judge_raw_output_path", "context_path",
    }
    for s in manifest["samples"]:
        for v in s["verdicts"]:
            assert set(v.keys()) == expected_keys, (
                f"unexpected keys {set(v.keys()) - expected_keys} or missing "
                f"{expected_keys - set(v.keys())} on verdict {v.get('sample_key')}"
            )

    # Strata sample_keys cover every inclusion-driving verdict; sibling
    # verdicts in the same rev folder have NO entry in any stratum's
    # sample_keys list.
    strata_keys = set()
    for st in manifest["strata"].values():
        strata_keys.update(st["sample_keys"])
        # No deprecated naming
        assert "formal_sample_keys" not in st

    sample_verdict_keys = {v["sample_key"] for s in manifest["samples"] for v in s["verdicts"]}
    # Every stratum sample_key must appear among the manifest's verdict entries
    assert strata_keys.issubset(sample_verdict_keys), (
        f"{len(strata_keys - sample_verdict_keys)} stratum keys missing from samples"
    )


def test_setup_census_fallback_when_pool_smaller_than_target(synthetic_data_root):
    """proj_c/no_match pool = 6 in the fixture; target=54 (floor=9) triggers census."""
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=54, seed=42)
    manifest = json.loads((data_root / "judge_validation" / "manifest.json").read_text())

    # proj_c/no_match expected to hit census (pool=6, floor=9)
    st = manifest["strata"]["proj_c/no_match"]
    assert st["pool_size"] == 6
    assert st["per_cell_target"] == 9
    assert st["census"] is True
    assert len(st["sample_keys"]) == 6  # take all

    # proj_a/match should NOT be census (pool=15 > floor=9)
    st_a = manifest["strata"]["proj_a/match"]
    assert st_a["pool_size"] == 15
    assert st_a["census"] is False
    assert len(st_a["sample_keys"]) == 9


# ---------------------------------------------------------------------------
# 5. Extension semantics
# ---------------------------------------------------------------------------


def test_extension_raises_floor_without_relabeling(synthetic_data_root):
    """Target raise extends each cell's sample keys; existing labels preserved."""
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)
    m1 = json.loads((data_root / "judge_validation" / "manifest.json").read_text())

    # Rater fills the first verdict in the first sample folder
    first = m1["samples"][0]
    first_sd = data_root / "judge_validation" / "samples" / first["sample_folder"]
    v = json.loads((first_sd / "verdicts.json").read_text())
    if v["verdicts"]:
        v["verdicts"][0]["verdict"] = "match"
        v["verdicts"][0]["matched_ai_comment_index"] = 0
        v["verdicts"][0]["reasoning"] = "rater call"
        (first_sd / "verdicts.json").write_text(json.dumps(v, indent=2))

    # Second pass: target=24 -> floor=4/stratum
    vmod.setup_validation(config=config, out_dir=data_root, target=24, seed=42)
    m2 = json.loads((data_root / "judge_validation" / "manifest.json").read_text())

    assert m2["pooled_target"] == 24
    assert m2["derived_per_cell_target_at_latest_setup"] == 4
    for k, st in m2["strata"].items():
        assert st["per_cell_target"] >= m1["strata"][k]["per_cell_target"]
        # prior sample keys must be a prefix of the new sample keys
        prior_keys = m1["strata"][k]["sample_keys"]
        new_keys = st["sample_keys"]
        assert new_keys[: len(prior_keys)] == prior_keys, \
            f"stratum {k}: prior sample keys not a prefix of new sample keys"

    # Rater label preserved byte-for-byte
    v2 = json.loads((first_sd / "verdicts.json").read_text())
    assert v2["verdicts"][0]["verdict"] == "match"
    assert v2["verdicts"][0]["reasoning"] == "rater call"


def test_extension_extends_strata_without_reprovisioning_existing_folders(synthetic_data_root):
    """A target raise that adds new sample keys whose rev folder already
    exists must NOT re-provision the folder; the stratum's sample_keys list
    grows but on-disk state is untouched."""
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=6, seed=42)
    m1 = json.loads((data_root / "judge_validation" / "manifest.json").read_text())
    samples_dir = data_root / "judge_validation" / "samples"
    # Snapshot mtime + content of every existing folder
    snapshot: dict[str, dict[str, float | str]] = {}
    for s in m1["samples"]:
        sd = samples_dir / s["sample_folder"]
        snapshot[s["sample_folder"]] = {
            "verdicts": (sd / "verdicts.json").read_text(),
            "sample_info": (sd / "sample_info.json").read_text(),
        }

    # Largest pool in fixture is 15; floor=16 forces census on every stratum,
    # which guarantees additional sample keys per stratum compared to floor=1
    # at target=6.
    vmod.setup_validation(config=config, out_dir=data_root, target=96, seed=42)
    m2 = json.loads((data_root / "judge_validation" / "manifest.json").read_text())

    # Every stratum is census after floor=16
    for k, st in m2["strata"].items():
        assert st["census"] is True, f"stratum {k} expected census after floor=16"
        prior_keys = m1["strata"][k]["sample_keys"]
        new_keys = st["sample_keys"]
        assert new_keys[: len(prior_keys)] == prior_keys
        assert len(new_keys) >= len(prior_keys)

    # Existing folder contents preserved byte-for-byte
    for folder, before in snapshot.items():
        sd = samples_dir / folder
        assert (sd / "verdicts.json").read_text() == before["verdicts"], \
            f"verdicts.json in {folder} mutated during extension"
        assert (sd / "sample_info.json").read_text() == before["sample_info"], \
            f"sample_info.json in {folder} mutated during extension"


# ---------------------------------------------------------------------------
# 6. Drift guards
# ---------------------------------------------------------------------------


def test_drift_guard_warns_on_frozen_field_change(synthetic_data_root, capsys):
    """Live verdicts.json mutating a verdict's verdict/reasoning field triggers DRIFT WARNING."""
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)
    manifest = json.loads((data_root / "judge_validation" / "manifest.json").read_text())
    sample = manifest["samples"][0]

    # Mutate reasoning (not the verdict itself, so the pool shape is preserved).
    proj = sample["project"]
    cond = sample["condition"]
    pr_dir = f"pr_{sample['pr_number']}"
    rev_dir = f"rev_{sample['head_sha'][:8]}"
    live_path = data_root / proj / "7_judgments" / cond / pr_dir / rev_dir / "verdicts.json"
    live = json.loads(live_path.read_text())
    live["verdicts"][0]["reasoning"] = "NEW REASONING"
    live_path.write_text(json.dumps(live, indent=2))

    vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)
    out = capsys.readouterr().out
    assert "DRIFT WARNING" in out


def test_drift_guard_fails_on_pool_membership_change(synthetic_data_root):
    """Flipping a sampled verdict's judge-verdict class (match<->no_match)
    shifts pool membership, which must cause setup to abort rather than silently
    re-stratify."""
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)
    manifest = json.loads((data_root / "judge_validation" / "manifest.json").read_text())

    # Pick the first verdict of the first sample as the target
    sample = manifest["samples"][0]
    frozen_v = sample["verdicts"][0]

    proj = sample["project"]
    cond = sample["condition"]
    pr_dir = f"pr_{sample['pr_number']}"
    rev_dir = f"rev_{sample['head_sha'][:8]}"
    live_path = data_root / proj / "7_judgments" / cond / pr_dir / rev_dir / "verdicts.json"
    live = json.loads(live_path.read_text())
    idx = frozen_v["human_comment_index"]
    for lv in live["verdicts"]:
        if lv["human_comment_index"] == idx:
            original = lv["verdict"]
            lv["verdict"] = "no_match" if original == "match" else "match"
            break
    live_path.write_text(json.dumps(live, indent=2))

    with pytest.raises(RuntimeError, match="not a prefix of the re-shuffled pool"):
        vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)


# ---------------------------------------------------------------------------
# 7. Compare: pooled + per-stratum + kappa
# ---------------------------------------------------------------------------


def _fill_verdicts_matching_judge(data_root: Path, manifest: dict) -> None:
    """Rater labels every verdict identically to the judge."""
    for sample in manifest["samples"]:
        sd = data_root / "judge_validation" / "samples" / sample["sample_folder"]
        v = json.loads((sd / "verdicts.json").read_text())
        frozen_by_idx = {fv["human_comment_index"]: fv for fv in sample["verdicts"]}
        for entry in v["verdicts"]:
            idx = entry["human_comment_index"]
            if idx in frozen_by_idx:
                entry["verdict"] = frozen_by_idx[idx]["judge_verdict_at_sampling"]
                entry["matched_ai_comment_index"] = 0 if entry["verdict"] == "match" else None
                entry["reasoning"] = "perfect rater"
        (sd / "verdicts.json").write_text(json.dumps(v, indent=2))


def test_compare_perfect_agreement(synthetic_data_root):
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)
    manifest = json.loads((data_root / "judge_validation" / "manifest.json").read_text())
    _fill_verdicts_matching_judge(data_root, manifest)

    vmod.compare_validation(config=config, out_dir=data_root)
    summary = json.loads((data_root / "judge_validation" / "agreement.json").read_text())
    assert summary["scheme"] == "verdict_level_case_cohort"
    assert summary["pooled"]["agreement"] == pytest.approx(1.0)
    assert summary["pooled"]["kappa"] == pytest.approx(1.0)
    assert summary["n_labeled"] == summary["n_expected"]

    # Per-stratum keys should all be (project/verdict_class)
    for k in summary["per_stratum"]:
        proj, verdict_class = k.split("/")
        assert proj in config.projects
        assert verdict_class in ("match", "no_match")


def test_compare_midlabeling_skips_nulls(synthetic_data_root):
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)
    manifest = json.loads((data_root / "judge_validation" / "manifest.json").read_text())

    # Label only the first sample folder
    first = manifest["samples"][0]
    sd = data_root / "judge_validation" / "samples" / first["sample_folder"]
    v = json.loads((sd / "verdicts.json").read_text())
    frozen_by_idx = {fv["human_comment_index"]: fv for fv in first["verdicts"]}
    for entry in v["verdicts"]:
        idx = entry["human_comment_index"]
        if idx in frozen_by_idx:
            entry["verdict"] = frozen_by_idx[idx]["judge_verdict_at_sampling"]
    (sd / "verdicts.json").write_text(json.dumps(v, indent=2))

    vmod.compare_validation(config=config, out_dir=data_root)
    summary = json.loads((data_root / "judge_validation" / "agreement.json").read_text())
    assert 0 < summary["n_labeled"] <= summary["n_expected"]
    # Pooled agreement on the labeled subset should be 1.0
    assert summary["pooled"]["agreement"] == pytest.approx(1.0)


def test_compare_under_coverage_matching_allows_duplicate_matched_ai_index(synthetic_data_root):
    """Under coverage-based matching (2026-05-04 judge-fix-2), one AI comment
    legitimately covers multiple human comments. The compare path must NOT
    flag repeated `matched_ai_comment_index` values as a labeling-integrity
    failure, and must NOT emit a `duplicate_warnings` key in agreement.json.
    """
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=18, seed=42)
    manifest = json.loads((data_root / "judge_validation" / "manifest.json").read_text())

    # Find a sample with at least 2 verdicts
    target = next((s for s in manifest["samples"] if len(s["verdicts"]) >= 2), None)
    if target is None:
        pytest.skip("no multi-verdict sample in fixture")

    sd = data_root / "judge_validation" / "samples" / target["sample_folder"]
    v = json.loads((sd / "verdicts.json").read_text())
    for entry in v["verdicts"][:2]:
        entry["verdict"] = "match"
        entry["matched_ai_comment_index"] = 0
        entry["reasoning"] = "coverage match: AI comment 0 covers both"
    (sd / "verdicts.json").write_text(json.dumps(v, indent=2))

    vmod.compare_validation(config=config, out_dir=data_root)
    summary = json.loads((data_root / "judge_validation" / "agreement.json").read_text())
    assert "duplicate_warnings" not in summary
    # Two coverage matches were labeled; both contribute to the labeled tally.
    assert summary["n_labeled"] >= 2
    assert summary["pooled"]["n"] >= 2

    report = (data_root / "judge_validation" / "agreement_report.md").read_text()
    assert "Rater labeling integrity" not in report
    assert "one-to-one" not in report


# ---------------------------------------------------------------------------
# 8. Status counts
# ---------------------------------------------------------------------------


def test_status_counts(synthetic_data_root, capsys):
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)
    vmod.status_validation(config=config, out_dir=data_root)
    out = capsys.readouterr().out
    assert "verdicts:" in out
    assert "folders:" in out
    assert "per-stratum" in out
    # Per-stratum line format: "    proj/verdict_class: labeled/expected"
    assert "/match:" in out or "/no_match:" in out


# ---------------------------------------------------------------------------
# 9. Atomic manifest write on abort
# ---------------------------------------------------------------------------


def test_atomic_manifest_write_survives_abort(synthetic_data_root, monkeypatch):
    data_root, config = synthetic_data_root
    vmod.setup_validation(config=config, out_dir=data_root, target=12, seed=42)
    manifest_path = data_root / "judge_validation" / "manifest.json"
    before = manifest_path.read_text()

    call_count = {"n": 0}
    original = vmod._provision_worktree

    def flaky_provision(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise subprocess.CalledProcessError(1, "git", stderr="synthetic failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(vmod, "_provision_worktree", flaky_provision)
    with pytest.raises(subprocess.CalledProcessError):
        vmod.setup_validation(config=config, out_dir=data_root, target=24, seed=42)

    after = manifest_path.read_text()
    assert after == before, "manifest.json mutated mid-abort"


# ---------------------------------------------------------------------------
# 10. CLI smoke test
# ---------------------------------------------------------------------------


def test_cli_smoke_validate_setup_compare_status(synthetic_data_root, monkeypatch):
    data_root, config = synthetic_data_root

    from reviewlore.experiment import cli as cli_mod

    def fake_setup(out, trial, force, yes, dry_run, backend_name=None):
        return config, data_root, None

    monkeypatch.setattr(cli_mod, "_setup", fake_setup)

    runner = CliRunner()
    result = runner.invoke(relox_cli, ["validate", "setup", "--target", "12", "--seed", "42"])
    assert result.exit_code == 0, result.output
    assert (data_root / "judge_validation" / "manifest.json").exists()

    result = runner.invoke(relox_cli, ["validate", "status"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(relox_cli, ["validate", "compare"])
    assert result.exit_code == 0, result.output
