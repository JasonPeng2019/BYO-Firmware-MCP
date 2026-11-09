"""Preserve the failing test's owned inputs before context teardown."""

from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import sys
import tempfile


def retain_directory(directory, destination):
    """Copy one owned temporary input tree without masking the test failure."""
    try:
        directory = Path(directory).resolve()
        destination = Path(destination).resolve()
        temporary = Path(tempfile.gettempdir()).resolve()
        if directory == temporary or not directory.is_relative_to(temporary):
            raise OSError("only owned temporary fixture inputs may be captured")
        if destination.is_relative_to(directory):
            raise OSError("failure destination must be outside the temporary fixture")
        shutil.copytree(directory, destination, dirs_exist_ok=True, symlinks=True)
        print(f"Persistent failing inputs/output: {destination}", file=sys.stderr)
        return True
    except OSError as error:
        print(f"Additional fixture capture error: {error}; original: {directory}", file=sys.stderr)
        return False


@contextmanager
def fixture_directory(prefix):
    directory = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    try:
        yield directory
    except BaseException:
        print(f"Failing fixture inputs/output retained at {directory}", file=sys.stderr)
        destination = (
            Path(
                os.environ.get(
                    "BYO_TEST_FAILURES", Path(__file__).resolve().parents[1] / ".test-failures"
                )
            )
            / directory.name
        )
        retain_directory(directory, destination)
        raise
    else:
        shutil.rmtree(directory)
