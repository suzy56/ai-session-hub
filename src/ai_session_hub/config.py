from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ai_session_hub.models import SourceSpec
from ai_session_hub.usage import PricingRule, amount

VALID_TOOLS = {
    "codex",
    "claude",
    "hermes",
    "omp",
    "dsh",
    "cursor-ide",
}


@dataclass
class AppConfig:
    data_dir: Path
    discover_defaults: bool = True
    sources: list[SourceSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pricing: tuple[PricingRule, ...] = ()


def is_path_overlap(p1: Path, p2: Path) -> bool:
    """Check if two paths are identical, or if one is an ancestor of the other."""
    c1 = p1.resolve()
    c2 = p2.resolve()
    return c1 == c2 or c1 in c2.parents or c2 in c1.parents


def discover_default_sources() -> list[SourceSpec]:
    """Discover conventional roots and environment-derived roots for all supported tools."""
    home = Path.home()
    app_support = home / "Library" / "Application Support"
    sources: list[SourceSpec] = []

    # 1. Codex
    codex_env = os.environ.get("CODEX_HOME")
    codex_root = Path(codex_env) if codex_env else home / ".codex"
    if codex_root.exists():
        sources.append(SourceSpec(tool="codex", root=codex_root))

    # 2. Claude Code
    claude_env = os.environ.get("CLAUDE_CONFIG_DIR")
    claude_root = Path(claude_env) if claude_env else home / ".claude"
    if claude_root.exists():
        sources.append(SourceSpec(tool="claude", root=claude_root))

    # 3. Hermes (default + profiles/*)
    hermes_env = os.environ.get("HERMES_HOME")
    hermes_root = Path(hermes_env) if hermes_env else home / ".hermes"
    if hermes_root.exists():
        sources.append(SourceSpec(tool="hermes", root=hermes_root, profile="default"))
        profiles_dir = hermes_root / "profiles"
        if profiles_dir.is_dir():
            for p_dir in profiles_dir.iterdir():
                if p_dir.is_dir() and not p_dir.name.startswith("."):
                    sources.append(SourceSpec(tool="hermes", root=p_dir, profile=p_dir.name))

    # 4. Oh My Pi (OMP)
    pi_config_env = os.environ.get("PI_CONFIG_DIR")
    if pi_config_env:
        # If relative, join with home as native resolver does
        omp_config_base = (home / pi_config_env).resolve() if not Path(pi_config_env).is_absolute() else Path(pi_config_env).resolve()
    else:
        omp_config_base = home / ".omp"

    omp_agent_env = os.environ.get("PI_CODING_AGENT_DIR")
    omp_default_agent = Path(omp_agent_env) if omp_agent_env else omp_config_base / "agent"
    omp_session_dir_env = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    omp_session_dir = Path(omp_session_dir_env) if omp_session_dir_env else None

    if omp_default_agent.exists():
        sources.append(
            SourceSpec(
                tool="omp",
                root=omp_default_agent,
                profile="default",
                sessions_dir=omp_session_dir,
            )
        )

    omp_profiles_dir = omp_config_base / "profiles"
    if omp_profiles_dir.is_dir():
        for p_dir in omp_profiles_dir.iterdir():
            if p_dir.is_dir() and not p_dir.name.startswith("."):
                agent_dir = p_dir / "agent"
                if agent_dir.is_dir():
                    sources.append(
                        SourceSpec(
                            tool="omp",
                            root=agent_dir,
                            profile=p_dir.name,
                        )
                    )

    # 5. DSH
    dsh_env = os.environ.get("DSH_HOME")
    dsh_root = Path(dsh_env) if dsh_env else home / ".dsh"
    if dsh_root.exists():
        sources.append(SourceSpec(tool="dsh", root=dsh_root))

    # 6. Cursor IDE
    cursor_root = app_support / "Cursor"
    if cursor_root.exists():
        sources.append(SourceSpec(tool="cursor-ide", root=cursor_root))

    return sources


def load_tokscale_pricing_catalog() -> list[PricingRule]:
    """Load pricing catalogs from Tokscale cache directory if present."""
    cache_dirs = [
        Path.home() / ".config" / "tokscale" / "cache",
        Path.home() / "Library" / "Caches" / "tokscale",
    ]
    pricing_files = [
        "pricing-litellm.json",
        "pricing-models-dev.json",
        "pricing-openrouter.json",
    ]
    rules: list[PricingRule] = []
    seen: set[tuple[str, str | None]] = set()

    for c_dir in cache_dirs:
        if not c_dir.is_dir():
            continue
        for fname in pricing_files:
            p = c_dir / fname
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8")).get("data", {})
                for model_key, rates in data.items():
                    if not isinstance(rates, dict):
                        continue
                    parts = model_key.split("/")
                    provider = parts[0] if len(parts) > 1 else None
                    model = parts[-1]

                    inp = rates.get("input_cost_per_token")
                    out = rates.get("output_cost_per_token")
                    cr = rates.get("cache_read_input_token_cost")
                    cw = rates.get("cache_creation_input_token_cost")

                    inp_m = round(inp * 1e6, 6) if (inp is not None and inp >= 0) else None
                    out_m = round(out * 1e6, 6) if (out is not None and out >= 0) else None
                    cr_m = round(cr * 1e6, 6) if (cr is not None and cr >= 0) else None
                    cw_m = round(cw * 1e6, 6) if (cw is not None and cw >= 0) else None

                    key = (model.lower(), provider.lower() if provider else None)
                    if key not in seen:
                        seen.add(key)
                        rules.append(PricingRule(model=model, provider=provider, input_per_million=inp_m, output_per_million=out_m, cache_read_per_million=cr_m, cache_write_per_million=cw_m))

                    bare_key = (model.lower(), None)
                    if bare_key not in seen:
                        seen.add(bare_key)
                        rules.append(PricingRule(model=model, provider=None, input_per_million=inp_m, output_per_million=out_m, cache_read_per_million=cr_m, cache_write_per_million=cw_m))
            except Exception:
                continue
    return rules


def load_config(config_path: Path | None = None, data_dir: Path | None = None) -> AppConfig:
    """Load configuration, validate paths, check data-dir isolation, and resolve sources."""
    # 1. Determine data directory
    if data_dir is None:
        effective_data_dir = Path.home() / "Library" / "Application Support" / "ai-session-hub"
    else:
        effective_data_dir = data_dir.expanduser().resolve()

    # 2. Determine config path
    explicit_config = config_path is not None
    if config_path is None:
        cfg_file = effective_data_dir / "config.toml"
    else:
        cfg_file = config_path.expanduser().resolve()
        if not cfg_file.is_file():
            raise FileNotFoundError(f"Configuration file not found: {cfg_file}")

    warnings: list[str] = []
    discover_defaults = True
    configured_sources: list[SourceSpec] = []
    pricing: list[PricingRule] = []

    # 3. Parse TOML if config file exists
    if cfg_file.is_file():
        try:
            with cfg_file.open("rb") as f:
                toml_data = tomllib.load(f)
        except Exception as e:
            raise ValueError(f"Invalid TOML in configuration file {cfg_file}: {e}") from e

        discover_defaults = bool(toml_data.get("discover_defaults", True))
        raw_pricing = toml_data.get("pricing", [])
        if not isinstance(raw_pricing, list):
            raise ValueError("Config 'pricing' must be an array of tables ([[pricing]])")
        seen_pricing: set[tuple[str, str | None]] = set()
        rate_fields = ("input_per_million", "output_per_million", "cache_read_per_million", "cache_write_per_million")
        for rule in raw_pricing:
            if not isinstance(rule, dict) or not isinstance(rule.get("model"), str) or not rule["model"].strip():
                raise ValueError("Every pricing rule requires an exact nonempty model name")
            provider = rule.get("provider")
            if provider is not None and (not isinstance(provider, str) or not provider.strip()):
                raise ValueError("Pricing provider must be a nonempty string when specified")
            unknown = set(rule) - {"model", "provider", *rate_fields}
            if unknown:
                raise ValueError(f"Unknown pricing fields: {sorted(unknown)}")
            key = (rule["model"], provider)
            if key in seen_pricing:
                raise ValueError(f"Duplicate pricing rule for {key}")
            seen_pricing.add(key)
            rates = {}
            for name in rate_fields:
                value = rule.get(name)
                if value is not None and amount(value) is None:
                    raise ValueError(f"Pricing {name} must be a finite nonnegative USD-per-million rate")
                rates[name] = amount(value)
            pricing.append(PricingRule(model=rule["model"], provider=provider, **rates))

        raw_sources = toml_data.get("sources", [])
        if not isinstance(raw_sources, list):
            raise ValueError("Config 'sources' must be an array of tables ([[sources]])")

        base_dir = cfg_file.parent

        for idx, src_item in enumerate(raw_sources):
            if not isinstance(src_item, dict):
                raise ValueError(f"Source #{idx + 1} must be a table")

            tool = src_item.get("tool")
            if tool == "token-monitor":
                raise ValueError("Token Monitor 数据源已移除；请删除该 [[sources]] 配置，直接配置原生工具目录。")
            if not tool or tool not in VALID_TOOLS:
                raise ValueError(
                    f"Source #{idx + 1}: unknown or missing tool '{tool}'. Valid tools: {sorted(VALID_TOOLS)}"
                )

            root_str = src_item.get("root")
            if not root_str:
                raise ValueError(f"Source #{idx + 1}: missing required 'root' path")

            root_path = Path(root_str).expanduser()
            if not root_path.is_absolute():
                root_path = (base_dir / root_path).resolve()
            else:
                root_path = root_path.resolve()

            profile = src_item.get("profile")

            sessions_dir_str = src_item.get("sessions_dir")
            sessions_dir_path = None
            if sessions_dir_str:
                if tool != "omp":
                    raise ValueError(f"Source #{idx + 1}: 'sessions_dir' is only valid for OMP sources, not '{tool}'")
                sessions_dir_path = Path(sessions_dir_str).expanduser()
                if not sessions_dir_path.is_absolute():
                    sessions_dir_path = (base_dir / sessions_dir_path).resolve()
                else:
                    sessions_dir_path = sessions_dir_path.resolve()

            if not root_path.exists():
                warnings.append(f"Configured source root for {tool} does not exist: {root_path}")

            configured_sources.append(
                SourceSpec(
                    tool=tool,
                    root=root_path,
                    profile=profile,
                    sessions_dir=sessions_dir_path,
                )
            )
    elif explicit_config:
        raise FileNotFoundError(f"Configuration file not found: {cfg_file}")

    # 4. Add default discovered sources if enabled
    all_sources: list[SourceSpec] = list(configured_sources)
    if discover_defaults:
        defaults = discover_default_sources()
        all_sources.extend(defaults)

    # 5. Deduplicate sources by (tool, canonical_root, profile)
    deduped_sources: list[SourceSpec] = []
    seen_keys: set[tuple[str, Path, str | None]] = set()

    for s in all_sources:
        key = (s.tool, s.canonical_root, s.profile)
        if key not in seen_keys:
            seen_keys.add(key)
            deduped_sources.append(s)

    # 6. Reject data-dir overlap with ANY effective storage root
    for s in deduped_sources:
        if is_path_overlap(effective_data_dir, s.canonical_root):
            raise ValueError(
                f"Data directory '{effective_data_dir}' overlaps with source root '{s.canonical_root}'"
            )
        if s.canonical_sessions_dir and is_path_overlap(effective_data_dir, s.canonical_sessions_dir):
            raise ValueError(
                f"Data directory '{effective_data_dir}' overlaps with OMP sessions_dir '{s.canonical_sessions_dir}'"
            )

    # Merge explicit user pricing with Tokscale pricing catalog
    tokscale_pricing = load_tokscale_pricing_catalog()
    user_keys = {(r.model.lower(), r.provider.lower() if r.provider else None) for r in pricing}
    user_bare = {r.model.lower() for r in pricing if r.provider is None}
    merged_pricing = list(pricing)
    for r in tokscale_pricing:
        k = (r.model.lower(), r.provider.lower() if r.provider else None)
        if k not in user_keys and not (r.provider is None and r.model.lower() in user_bare):
            merged_pricing.append(r)

    return AppConfig(
        data_dir=effective_data_dir,
        discover_defaults=discover_defaults,
        sources=deduped_sources,
        warnings=warnings,
        pricing=tuple(merged_pricing),
    )
