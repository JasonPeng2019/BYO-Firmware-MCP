"""Preserve the failing test's owned inputs before context teardown."""

from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import sys
import tempfile


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
        try:
            if destination.resolve().is_relative_to(directory):
                raise OSError("failure destination must be outside the temporary fixture")
            shutil.copytree(directory, destination, dirs_exist_ok=True)
            print(f"Persistent failing inputs/output: {destination}", file=sys.stderr)
        except OSError as error:
            print(f"Additional fixture capture error: {error}; original retained", file=sys.stderr)
        raise
    else:
        shutil.rmtree(directory)
