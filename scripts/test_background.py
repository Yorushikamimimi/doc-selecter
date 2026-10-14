"""Run the reviewed background subset in a fresh artificial sandbox."""
from pathlib import Path
import json
import os
import tempfile
import unittest


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def main():
    repo = Path(__file__).resolve().parents[1]
    os.chdir(repo)
    root = Path(tempfile.mkdtemp(prefix="docsel-background-", dir="/private/tmp")).resolve()
    for name in ("Downloads", "state", "tmp", "records"):
        (root / name).mkdir()
    os.environ["DOCSELECTER_TEST_ROOT"] = str(root)
    os.environ["TMPDIR"] = str(root / "tmp")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    tempfile.tempdir = str(root / "tmp")
    loader = unittest.TestLoader()
    items = list(flatten(loader.discover("tests", top_level_dir=str(repo))))
    if loader.errors:
        raise RuntimeError("Test discovery failed: " + str(loader.errors))
    background, not_run = [], []
    for item in items:
        if item.id().startswith("tests.test_gui") or "gui" in type(item).__name__.lower():
            not_run.append(item.id())
        else:
            background.append(item)
    with (root / "records/background-tests.log").open("w", encoding="utf-8") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(unittest.TestSuite(background))
    summary = {"total_ids": len(items), "run_ids": [x.id() for x in background],
               "not_run_gui_ids": not_run, "run": result.testsRun,
               "failure": len(result.failures), "error": len(result.errors),
               "skip": len(result.skipped), "successful": result.wasSuccessful()}
    (root / "records/summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Evidence:", root / "records")
    print({k: v for k, v in summary.items() if not k.endswith("_ids")}, "GUI not run:", len(not_run))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
