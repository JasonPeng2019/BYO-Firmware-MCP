"""Runtime resource resolution for checkout and packaged installs."""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PACKAGED_DATA_ROOT = PACKAGE_ROOT / "_runtime_data"  # PROJECT-DEFINED (bundled runtime data root)


class RuntimeResourceError(RuntimeError):
    """Raised when required packaged or checkout runtime data is missing."""


def resolve_repo_root(*, required: bool = False) -> Path | None:
    """Return the live checkout root when running from a repo, else ``None``."""

    candidate = PACKAGE_ROOT.parents[1]
    if (candidate / "pyproject.toml").exists() and (candidate / "src" / "pyocd_debug_mcp").exists():
        return candidate
    if required:
        raise RuntimeResourceError(
            "This operation requires a live repo checkout. No Firmware-CLI repo root was detected "
            f"near {PACKAGE_ROOT}."
        )
    return None


def _resolve_root(
    *,
    repo_relative: tuple[str, ...],
    packaged_relative: tuple[str, ...],
) -> Path:
    repo_root = resolve_repo_root(required=False)
    if repo_root is not None:
        repo_path = repo_root.joinpath(*repo_relative)
        if repo_path.exists():
            return repo_path
    packaged_path = PACKAGED_DATA_ROOT.joinpath(*packaged_relative)
    if packaged_path.exists():
        return packaged_path
    raise RuntimeResourceError(
        "Required runtime resource path was not found in either the repo checkout or the packaged "
        f"bundle: repo={Path(*repo_relative)} packaged={Path(*packaged_relative)}"
    )


def resolve_benchmark_cases_root() -> Path:
    return _resolve_root(
        repo_relative=("tests", "cases"),
        packaged_relative=("benchmark", "cases"),
    )


def resolve_benchmark_result_schema_path() -> Path:
    return _resolve_root(
        repo_relative=("tests", "cases", "r11_result_schema.json"),
        packaged_relative=("benchmark", "cases", "r11_result_schema.json"),
    )
