"""Run independent tests against isolated restorations; never edit candidate src.

Default restoration removes only the forced auto_unlock=False option. A second
mutation removes warning-based refusal while preserving the no-erase setting.
Both must fail the locked public test with a successful unlocked workflow control.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import sys


SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from tests.m01_s01_evidence import EVIDENCE, run  # noqa: E402


def main() -> int:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "initial"
    source = SERVER / "src/pyocd_debug_mcp/adapters/swd_pyocd.py"
    original = source.read_text(encoding="utf-8")
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    outcomes = []
    for mutation in ("restore-default", "remove-refusal"):
        destination = EVIDENCE / "mutations" / prefix / mutation
        shutil.copytree(
            SERVER / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__")
        )
        changed = destination / "src/pyocd_debug_mcp/adapters/swd_pyocd.py"
        if mutation == "restore-default":
            before = 'options: dict[str, object] = {"auto_unlock": False}'
            assert original.count(before) == 1
            modified = original.replace(before, "options: dict[str, object] = {}")
        else:
            node = next(
                node
                for node in ast.parse(original).body
                if isinstance(node, ast.FunctionDef)
                and node.name == "_open_refusing_protected_target"
            )
            lines = original.splitlines(keepends=True)
            modified = "".join(
                lines[: node.lineno - 1]
                + [
                    "def _open_refusing_protected_target(session: object) -> None:\n",
                    '    getattr(session, "open")()\n',
                ]
                + lines[node.end_lineno :]
            )
        changed.write_text(modified, encoding="utf-8", newline="\n")
        (destination / "change.diff").write_text(
            "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    modified.splitlines(keepends=True),
                    fromfile="candidate/swd_pyocd.py",
                    tofile=f"{mutation}/swd_pyocd.py",
                )
            ),
            encoding="utf-8",
        )
        for control in (False, True):
            label = f"{prefix}-{mutation}-{'control' if control else 'locked'}"
            method = (
                "test_unlocked_public_connect_read_disconnect_control"
                if control
                else "test_public_locked_connect_and_reset_refuse_without_any_write"
            )
            bootstrap = destination / f"{label}.py"
            bootstrap.write_text(
                "import json,sys,unittest\nfrom pathlib import Path\n"
                f"sys.path[:0] = {[str(destination / 'src'), str(SERVER)]!r}\n"
                "from tests.test_m01_s01_public_session import import_identities\n"
                "from pyocd_debug_mcp.adapters import swd_pyocd\n"
                f"assert Path(swd_pyocd.__file__).resolve() == Path({str(changed)!r}).resolve()\n"
                "print(json.dumps(import_identities(), indent=2), flush=True)\n"
                f"suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_m01_s01_public_session.PublicSessionProof.{method}')\n"
                "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
                "print(json.dumps(dict(run=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped))))\n"
                "raise SystemExit(0 if result.wasSuccessful() else 1)\n",
                encoding="utf-8",
            )
            raw_exit = run(label, [sys.executable, str(bootstrap)])
            outcomes.append(dict(mutation=mutation, control=control, raw_exit=raw_exit))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    (EVIDENCE / f"{prefix}-sensitivity-outcomes.json").write_text(
        json.dumps(outcomes, indent=2) + "\n", encoding="utf-8"
    )
    # This exit validates the experiment, not the deliberately broken candidate.
    return 0 if all(item["raw_exit"] == (0 if item["control"] else 1) for item in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
