"""YAML config loading into dataclasses for ReviewLore."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class PromptConfig:
    path: str
    version: int


@dataclass
class BackendConfig:
    name: str
    claude_code: dict = field(default_factory=dict)
    replay: dict = field(default_factory=dict)


@dataclass
class OptimizerConfig:
    batch_size: int = 10
    snapshot_interval: int = 25


@dataclass
class ConcurrencyConfig:
    extraction_workers: int = 2
    review_workers: int = 2
    worker_stagger_delay: int = 30


@dataclass
class ActionabilityConfig:
    model: str = "claude-sonnet-4-6"
    budget: float = 50.0


@dataclass
class FilterConfig:
    bot_usernames: list[str] = field(default_factory=list)
    min_word_count: int = 3
    exclude_patterns: list[str] = field(default_factory=list)
    actionability: ActionabilityConfig = field(default_factory=ActionabilityConfig)


@dataclass
class LabelConfig:
    model: str = "claude-sonnet-4-6"
    budget: float = 50.0


@dataclass
class CollectionConfig:
    rate_limit_delay: float = 1.0
    retry_max: int = 3
    retry_base_delay: float = 2.0
    min_review_threads: int = 1
    target_mrs: int = 250


@dataclass
class SplitConfig:
    tuning_ratio: float = 0.8


@dataclass
class ProjectConfig:
    platform: str
    repo: str
    language: str
    subject_repo: str = ""


@dataclass
class Config:
    output_dir: str
    backend: BackendConfig
    models: dict[str, str]
    budgets: dict[str, float]
    prompts: dict[str, PromptConfig]
    optimizer: OptimizerConfig
    concurrency: ConcurrencyConfig
    filter: FilterConfig
    label: LabelConfig
    collection: CollectionConfig
    split: SplitConfig
    conditions: list[str]
    projects: dict[str, ProjectConfig]
    config_dir: Path = field(default_factory=lambda: Path("."))


def load_config(path: str | Path | None = None) -> Config:
    """Load reviewlore.yaml into a Config dataclass.

    Search order for config file:
    1. Explicit path argument
    2. REVIEWLORE_CONFIG env var
    3. reviewlore.yaml in current directory
    """
    if path is None:
        path = os.environ.get("REVIEWLORE_CONFIG", "reviewlore.yaml")
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    config_dir = path.parent.resolve()

    # Parse nested structures
    backend_raw = raw.get("backend", {})
    backend = BackendConfig(
        name=backend_raw.get("name", "claude-code"),
        claude_code=backend_raw.get("claude_code", {}),
        replay=backend_raw.get("replay", {}),
    )

    prompts = {}
    for name, pdata in raw.get("prompts", {}).items():
        prompts[name] = PromptConfig(path=pdata["path"], version=pdata["version"])

    opt_raw = raw.get("optimizer", {})
    optimizer = OptimizerConfig(
        batch_size=opt_raw.get("batch_size", 10),
        snapshot_interval=opt_raw.get("snapshot_interval", 25),
    )

    conc_raw = raw.get("concurrency", {})
    concurrency = ConcurrencyConfig(
        extraction_workers=conc_raw.get("extraction_workers", 2),
        review_workers=conc_raw.get("review_workers", 2),
        worker_stagger_delay=conc_raw.get("worker_stagger_delay", 30),
    )

    filt_raw = raw.get("filter", {})
    act_raw = filt_raw.get("actionability", {})
    filter_cfg = FilterConfig(
        bot_usernames=filt_raw.get("bot_usernames", []),
        min_word_count=filt_raw.get("min_word_count", 3),
        exclude_patterns=filt_raw.get("exclude_patterns", []),
        actionability=ActionabilityConfig(
            model=act_raw.get("model", "claude-sonnet-4-6"),
            budget=act_raw.get("budget", 50.0),
        ),
    )

    label_raw = raw.get("label", {})
    label_cfg = LabelConfig(
        model=label_raw.get("model", "claude-sonnet-4-6"),
        budget=label_raw.get("budget", 50.0),
    )

    coll_raw = raw.get("collection", {})
    collection = CollectionConfig(
        rate_limit_delay=coll_raw.get("rate_limit_delay", 1.0),
        retry_max=coll_raw.get("retry_max", 3),
        retry_base_delay=coll_raw.get("retry_base_delay", 2.0),
        min_review_threads=coll_raw.get("min_review_threads", 1),
        target_mrs=coll_raw.get("target_mrs", 250),
    )

    split_raw = raw.get("split", {})
    split_cfg = SplitConfig(
        tuning_ratio=split_raw.get("tuning_ratio", 0.8),
    )

    projects = {}
    for name, pdata in raw.get("projects", {}).items():
        projects[name] = ProjectConfig(
            platform=pdata["platform"],
            repo=pdata["repo"],
            language=pdata["language"],
            subject_repo=pdata.get("subject_repo", ""),
        )

    return Config(
        output_dir=raw.get("output_dir", "data/"),
        backend=backend,
        models=raw.get("models", {}),
        budgets=raw.get("budgets", {}),
        prompts=prompts,
        optimizer=optimizer,
        concurrency=concurrency,
        filter=filter_cfg,
        label=label_cfg,
        collection=collection,
        split=split_cfg,
        conditions=raw.get("conditions", ["generic"]),
        projects=projects,
        config_dir=config_dir,
    )


def resolve_output_dir(cli_flag: str | None, config: Config, trial: bool = False) -> Path:
    """Resolve data output directory.

    Precedence: CLI -o flag > RELO_OUT env var > config output_dir.

    When ``trial`` is True, the resolved path gets a ``-trial`` suffix
    appended (e.g., ``data`` -> ``data-trial``) so trial runs do not
    overwrite production outputs. This is applied uniformly regardless
    of the source (CLI / env / config); pass an explicit ``--out`` to
    opt out.
    """
    if cli_flag:
        base = Path(cli_flag)
    else:
        env_val = os.environ.get("RELO_OUT")
        base = Path(env_val) if env_val else config.config_dir / config.output_dir
    if trial:
        return base.parent / f"{base.name}-trial" if base.parent != Path() else Path(f"{base.name}-trial")
    return base


def resolve_project(name: str, config: Config) -> ProjectConfig:
    """Validate and return project config by name."""
    if name not in config.projects:
        available = ", ".join(config.projects.keys())
        raise ValueError(f"Unknown project '{name}'. Available: {available}")
    return config.projects[name]


def get_prompt(name: str, config: Config) -> str:
    """Read prompt file contents by prompt config name."""
    if name not in config.prompts:
        raise KeyError(f"No prompt config for '{name}'")
    prompt_path = config.config_dir / config.prompts[name].path
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    return prompt_path.read_text()


def get_prompt_version(name: str, config: Config) -> int:
    """Get the version number for a prompt."""
    if name not in config.prompts:
        raise KeyError(f"No prompt config for '{name}'")
    return config.prompts[name].version

