#!/usr/bin/env python3
"""Verify that retained release-candidate logs describe one current source tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "sentry-evidence" / "results"
RELEASE_LOGS = (
    RESULTS / "0.1.8rc1-static.log",
    RESULTS / "0.1.8rc1-python310-parse.log",
    RESULTS / "0.1.8rc1-software-suite.log",
    RESULTS / "0.1.8rc1-monitor-codex-e2e.log",
)
RELEASE_LOG_RELATIVES = frozenset(path.relative_to(ROOT).as_posix() for path in RELEASE_LOGS)
RELEASE_LOG_PATTERN = "0.1.8rc1-*.log"
STATUS_PATTERN = re.compile(r"(?m)^(?:overall_)?exit_status=([0-9]+)$")
TEST_COUNT_PATTERN = re.compile(r"(?m)^Ran ([0-9][0-9,]*) tests? in ")
PARSED_COUNT_PATTERN = re.compile(r"(?m)^parsed_files=([0-9][0-9,]*)$")
ATTESTATION = RESULTS / "0.1.8rc1-attestation.json"
ATTESTATION_RELATIVE = ATTESTATION.relative_to(ROOT).as_posix()
GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")


def _release_logs(results_dir: Path) -> tuple[Path, ...]:
    """Return the canonical release-log names rooted in ``results_dir``."""

    return tuple(results_dir / path.name for path in RELEASE_LOGS)


def _attestation(results_dir: Path) -> Path:
    return results_dir / ATTESTATION.name


def _regular_file_error(path: Path, description: str) -> str | None:
    """Reject missing, symlinked, and non-regular release evidence paths."""

    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return f"missing {description}: {path}"
    except OSError as exc:
        return f"cannot inspect {description} {path}: {exc}"
    if stat.S_ISLNK(mode):
        return f"{description} must not be a symlink: {path}"
    try:
        metadata = path.lstat()
    except OSError as exc:
        return f"cannot inspect {description} {path}: {exc}"
    if _is_reparse_point(metadata):
        return f"{description} must not be a Windows reparse point: {path}"
    if not stat.S_ISREG(mode):
        return f"{description} is not a regular file: {path}"
    if metadata.st_nlink != 1:
        return f"{description} must not be multiply linked: {path}"
    return None


def _read_regular_file(path: Path, description: str) -> bytes:
    """Open, validate, and read one evidence leaf through the same descriptor."""

    error = _regular_file_error(path, description)
    if error is not None:
        raise ValueError(error)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot open {description} {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_ISLNK(current.st_mode)
            or _is_reparse_point(current)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError(f"{description} changed while it was being opened: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(attributes & marker) or bool(getattr(metadata, "st_reparse_tag", 0))


def _descriptor_directory() -> Path | None:
    """Return a pathname view of open descriptors, when safely available."""

    if os.name != "posix":
        return None
    for candidate in (Path("/proc/self/fd"), Path("/dev/fd")):
        if candidate.is_dir():
            return candidate
    return None


def _directory_chain_error(path: Path, description: str) -> str | None:
    """Reject a results root (or ancestor) that can redirect evidence access."""

    absolute = Path(os.path.abspath(path))
    for component in reversed((absolute, *absolute.parents)):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            return f"missing {description}: {component}"
        except OSError as exc:
            return f"cannot inspect {description} {component}: {exc}"
        if stat.S_ISLNK(metadata.st_mode):
            return f"{description} must not be a symlink: {component}"
        if _is_reparse_point(metadata):
            return f"{description} must not be a Windows reparse point: {component}"
        if not stat.S_ISDIR(metadata.st_mode):
            return f"{description} has a non-directory component: {component}"
    return None


@contextmanager
def pinned_release_directory(directory: Path, *, create: bool = False) -> Iterator[Path]:
    """Yield a no-follow, identity-pinned directory path for evidence I/O.

    POSIX operations use ``/proc/self/fd`` or ``/dev/fd`` so replacing the public
    results path cannot redirect an in-flight read or publication. Platforms
    without descriptor-path support fail closed before evidence access.
    """

    public = Path(os.path.abspath(directory))
    if create:
        public.mkdir(parents=True, exist_ok=True)
    error = _directory_chain_error(public, "release results directory")
    if error is not None:
        raise ValueError(error)
    before = public.lstat()
    identity = (before.st_dev, before.st_ino)
    descriptor_directory = _descriptor_directory()
    if descriptor_directory is None:
        raise OSError(
            "safe release evidence access requires POSIX descriptor-path support "
            "(/proc/self/fd or /dev/fd)"
        )
    descriptor = os.open(
        public,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != identity:
        os.close(descriptor)
        raise ValueError("release results directory changed while it was opened")
    access = descriptor_directory / str(descriptor)
    try:
        yield access
        error = _directory_chain_error(public, "release results directory")
        if error is not None:
            raise ValueError(error)
        after = public.lstat()
        if (after.st_dev, after.st_ino) != identity:
            raise ValueError("release results directory changed during evidence access")
    finally:
        os.close(descriptor)


@contextmanager
def pinned_release_directories(
    directories: Sequence[Path],
    *,
    on_validation_failure: Callable[[], None] | None = None,
) -> Iterator[tuple[Path, ...]]:
    """Pin several directories and keep every anchor open through validation.

    A multi-directory publication may need both its staging and destination
    handles to roll back if either public pathname is exchanged at the final
    identity check. Nested single-directory context managers close the inner
    handle too early for that rollback, so this transaction-oriented variant
    validates the full set before closing any descriptor.
    """

    pins: list[tuple[Path, Path, tuple[int, int], int]] = []
    descriptor_directory = _descriptor_directory()
    if descriptor_directory is None:
        raise OSError(
            "safe release evidence access requires POSIX descriptor-path support "
            "(/proc/self/fd or /dev/fd)"
        )
    try:
        for directory in directories:
            public = Path(os.path.abspath(directory))
            error = _directory_chain_error(public, "release results directory")
            if error is not None:
                raise ValueError(error)
            before = public.lstat()
            identity = (before.st_dev, before.st_ino)
            descriptor = os.open(
                public,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != identity:
                os.close(descriptor)
                raise ValueError("release results directory changed while it was opened")
            access = descriptor_directory / str(descriptor)
            pins.append((public, access, identity, descriptor))

        yield tuple(access for _public, access, _identity, _descriptor in pins)

        try:
            for public, _access, identity, _descriptor in pins:
                error = _directory_chain_error(public, "release results directory")
                if error is not None:
                    raise ValueError(error)
                after = public.lstat()
                if (after.st_dev, after.st_ino) != identity:
                    raise ValueError("release results directory changed during evidence access")
        except BaseException:
            if on_validation_failure is not None:
                on_validation_failure()
            raise
    finally:
        for _public, _access, _identity, descriptor in reversed(pins):
            os.close(descriptor)


def _write_release_text_in(directory: Path, name: str, content: str) -> None:
    """Atomically replace ``name`` below an already pinned directory."""

    path = directory / name
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(handle.fileno(), 0o644)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_release_text(path: Path, content: str) -> None:
    """Replace one release file without following an existing destination link."""

    with pinned_release_directory(path.parent, create=True) as directory:
        _write_release_text_in(directory, path.name, content)


def _candidate_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    candidates: list[str] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8", "surrogateescape")
        if relative in RELEASE_LOG_RELATIVES or relative == ATTESTATION_RELATIVE:
            continue
        candidates.append(relative)
    return sorted(candidates)


def _tracked_modes() -> dict[str, str]:
    """Return canonical Git index modes for tracked candidate paths."""

    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    modes: dict[str, str] = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0].decode("ascii")
        modes[raw_path.decode("utf-8", "surrogateescape")] = mode
    return modes


def _manifest_mode(relative: str, path: Path, tracked_modes: dict[str, str]) -> bytes:
    """Return a portable type/mode marker for one manifest entry.

    Git's index mode is authoritative for tracked files. Worktree permission
    bits are intentionally ignored because checkout umasks and
    ``core.filemode`` differ across hosts. Untracked release candidates still
    use their actual executable bit, which is the mode Git would record when
    they are added.
    """

    mode = tracked_modes.get(relative)
    if mode == "120000":
        return b"symlink-mode\0"
    if mode == "100755":
        return b"executable\0"
    if mode is not None:
        return b"regular\0"
    return b"executable\0" if path.lstat().st_mode & 0o111 else b"regular\0"


def working_tree_manifest_sha256() -> str:
    """Hash paths, portable modes, and bytes for the candidate source handoff."""

    digest = hashlib.sha256()
    tracked_modes = _tracked_modes()
    for relative in _candidate_paths():
        path = ROOT / relative
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        if not path.exists() and not path.is_symlink():
            digest.update(b"deleted\0")
            continue
        digest.update(_manifest_mode(relative, path, tracked_modes))
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(path.readlink().as_posix().encode("utf-8", "surrogateescape"))
        elif path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(b"non-file")
        digest.update(b"\0")
    return digest.hexdigest()


def _metadata(text: str) -> dict[str, str]:
    header = text.split("--- output ---", 1)[0]
    values: dict[str, str] = {}
    for line in header.splitlines()[1:]:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _valid_base_git_head(value: str | None) -> bool:
    """Accept only a real commit that is the candidate HEAD or its ancestor."""

    if value is None or GIT_OBJECT_ID.fullmatch(value) is None:
        return False
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{value}^{{commit}}"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode != 0:
        return False
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", value, "HEAD"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return ancestor.returncode == 0


def _expected_attestation_in(results_dir: Path) -> dict[str, object]:
    """Describe logs below an already pinned results directory."""

    logs = []
    for canonical, path in zip(RELEASE_LOGS, _release_logs(results_dir), strict=True):
        content = _read_regular_file(path, "release evidence log")
        logs.append(
            {
                "path": canonical.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return {
        "schema_version": 1,
        "candidate_manifest_sha256": working_tree_manifest_sha256(),
        "release_logs": logs,
    }


def expected_attestation(results_dir: Path = RESULTS) -> dict[str, object]:
    """Describe the exact retained logs without creating a manifest cycle."""

    with pinned_release_directory(results_dir) as directory:
        return _expected_attestation_in(directory)


def write_attestation(results_dir: Path = RESULTS) -> None:
    """Atomically attest the final release logs and candidate manifest."""

    with pinned_release_directory(results_dir) as directory:
        _write_release_text_in(
            directory,
            ATTESTATION.name,
            json.dumps(_expected_attestation_in(directory), indent=2, sort_keys=True) + "\n",
        )


def _verify_release_logs_in(results_dir: Path) -> list[str]:
    """Verify logs below an already identity-pinned results directory."""

    errors: list[str] = []
    release_logs = _release_logs(results_dir)
    attestation_path = _attestation(results_dir)
    unexpected_logs = sorted(set(results_dir.glob(RELEASE_LOG_PATTERN)) - set(release_logs))
    for path in unexpected_logs:
        try:
            shown = path.relative_to(ROOT)
        except ValueError:
            shown = path
        errors.append(f"unexpected release evidence log: {shown}")
    texts: dict[Path, str] = {}
    metadata: dict[Path, dict[str, str]] = {}
    for path in release_logs:
        try:
            text = _read_regular_file(path, "release evidence log").decode("utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"cannot read release evidence {path}: {exc}")
            continue
        texts[path] = text
        metadata[path] = _metadata(text)
        statuses = STATUS_PATTERN.findall(text)
        if not statuses:
            errors.append(f"{path.name}: no exit status")
        elif any(status != "0" for status in statuses):
            errors.append(f"{path.name}: nonzero exit status")
        if "FAILED (" in text or "FAILED\n" in text:
            errors.append(f"{path.name}: failed test marker")
        for key in ("base_git_head", "working_tree_manifest_sha256", "uv_lock_sha256"):
            if key not in metadata[path]:
                errors.append(f"{path.name}: missing {key}")

    if len(metadata) != len(release_logs):
        return errors

    for key in ("base_git_head", "working_tree_manifest_sha256", "uv_lock_sha256"):
        values = {values.get(key) for values in metadata.values()}
        if len(values) != 1:
            errors.append(f"release logs disagree on {key}")

    recorded_base = next(iter(metadata.values())).get("base_git_head")
    if not _valid_base_git_head(recorded_base):
        errors.append("release logs do not name an ancestor base Git commit")

    expected_manifest = working_tree_manifest_sha256()
    recorded_manifest = next(iter(metadata.values())).get("working_tree_manifest_sha256")
    if recorded_manifest != expected_manifest:
        errors.append("release logs do not match the current candidate source manifest")

    expected_lock = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    recorded_lock = next(iter(metadata.values())).get("uv_lock_sha256")
    if recorded_lock != expected_lock:
        errors.append("release logs do not match the current uv.lock")

    software = texts.get(results_dir / "0.1.8rc1-software-suite.log", "")
    live = texts.get(results_dir / "0.1.8rc1-monitor-codex-e2e.log", "")
    if "\nOK" not in software:
        errors.append("software-suite log has no unittest OK marker")
    if "\nOK" not in live:
        errors.append("monitor Codex E2E log has no unittest OK marker")

    release_status = (ROOT / "docs" / "RELEASE_STATUS.md").read_text(encoding="utf-8")
    software_count = TEST_COUNT_PATTERN.search(software)
    if software_count is None:
        errors.append("software-suite log has no test count")
    else:
        count = f"{int(software_count.group(1).replace(',', '')):,}"
        if f"Full default {count}-test software suite" not in release_status:
            errors.append("release status test count disagrees with the software-suite log")
    live_count = TEST_COUNT_PATTERN.search(live)
    if live_count is None or live_count.group(1) != "22":
        errors.append("monitor Codex E2E log must retain the claimed 22 module tests")

    parse_log = texts.get(results_dir / "0.1.8rc1-python310-parse.log", "")
    parsed_count = PARSED_COUNT_PATTERN.search(parse_log)
    if parsed_count is None:
        errors.append("Python 3.10 parse log has no parsed_files count")
    else:
        count = f"{int(parsed_count.group(1).replace(',', '')):,}"
        if f"All {count} source files parsed" not in release_status:
            errors.append("release status parse count disagrees with the Python 3.10 log")

    attestation_error = _regular_file_error(attestation_path, "release attestation")
    if attestation_error is not None:
        errors.append(attestation_error)
    else:
        try:
            attestation = json.loads(
                _read_regular_file(attestation_path, "release attestation").decode("utf-8")
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
            errors.append(f"invalid release attestation: {exc}")
        else:
            try:
                expected = _expected_attestation_in(results_dir)
            except (OSError, ValueError) as exc:
                errors.append(f"cannot compute expected release attestation: {exc}")
            else:
                if attestation != expected:
                    errors.append(
                        "release attestation does not match the retained logs and candidate"
                    )
    return errors


def verify_release_logs(results_dir: Path = RESULTS) -> list[str]:
    """Return all release-evidence consistency errors without mutating logs."""

    try:
        with pinned_release_directory(results_dir) as directory:
            return _verify_release_logs_in(directory)
    except (OSError, ValueError) as exc:
        return [str(exc)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-attestation",
        action="store_true",
        help="atomically replace the attestation after regenerating all release logs",
    )
    arguments = parser.parse_args(argv)
    if arguments.write_attestation:
        write_attestation()
    errors = verify_release_logs()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"release evidence verification passed ({len(RELEASE_LOGS)} consistent logs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
