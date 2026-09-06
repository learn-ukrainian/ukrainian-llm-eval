"""Reproducible tool restrictions for the exact bundled native model entry.

The native catalog can select tools independently of feature flags. Preserve
every model/instruction field except this explicit, hashed tool policy. Never
refresh a catalog from the network, invent a model entry, or change its alias.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from . import adapters, native_codex

TOOL_POLICY = {
    "tool_mode": None,
    "multi_agent_version": None,
    "apply_patch_tool_type": None,
    "experimental_supported_tools": [],
    "supports_search_tool": False,
}


def restrict_catalog(catalog: Any, model: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Keep one exact entry; fail if the installed metadata contract is unknown."""
    if not isinstance(catalog, dict) or set(catalog) != {"models"} or not isinstance(catalog["models"], list):
        raise native_codex._fail("native model catalog is invalid")
    matches = [entry for entry in catalog["models"] if isinstance(entry, dict) and entry.get("slug") == model]
    if len(matches) != 1 or not TOOL_POLICY.keys() <= matches[0].keys():
        raise native_codex._fail("native model catalog entry is missing, ambiguous, or unsupported")
    original = matches[0]
    # No aliases, instructions, context limits, supported efforts or backend
    # options are rewritten. The complete before/after entry hashes are retained.
    restricted = {**original, **TOOL_POLICY}
    return {"models": [restricted]}, {
        "bundled_model_entry_sha256": adapters.digest(original),
        "restricted_model_entry_sha256": adapters.digest(restricted),
        "catalog_tool_policy_sha256": adapters.digest(TOOL_POLICY),
    }


def bundled_catalog(probe: native_codex._CliProbe, model: str, timeout: int) -> tuple[dict, dict]:
    """Capture offline metadata in an empty home using bounded subprocess I/O."""
    with tempfile.TemporaryDirectory(prefix="codex-catalog-") as temp:
        root = Path(temp)
        root.chmod(0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        env = {key: value for key, value in native_codex.os.environ.items() if key in native_codex._SAFE_CHILD_ENV}
        env.update(HOME=str(home), CODEX_HOME=str(home / "codex-home"))
        Path(env["CODEX_HOME"]).mkdir(mode=0o700)
        result = native_codex._run_process(
            [str(probe.binary_path), "debug", "models", "--bundled"],
            cwd=root, env=env, prompt="", timeout=min(timeout, 15), evidence=None,
        )
        if result.returncode != 0:
            raise native_codex._fail("native bundled model catalog unavailable")
        return restrict_catalog(adapters._strict_json_loads(result.stdout), model)


def write_catalog(path: Path, catalog: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(adapters.canonical(catalog))
    path.chmod(0o600)


def build_argv(binary: str, *, model: str, effort: str, response_schema_path: Path,
               catalog_path: Path, reference_overrides: tuple[str, ...] = (),
               transport_overrides: tuple[str, ...] = ()) -> list[str]:
    """Both conditions use the same catalog restrictions and disabled host."""
    return native_codex.build_closed_book_argv(
        binary, model=model, effort=effort, response_schema_path=response_schema_path,
        transport_overrides=(
            "-c", "model_catalog_json=" + adapters.canonical(str(catalog_path)),
            "-c", "tools.experimental_request_user_input.enabled=false",
            *reference_overrides, *transport_overrides,
        ),
    )
