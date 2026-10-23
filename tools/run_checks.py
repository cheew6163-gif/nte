"""Run the offline suite, retaining generated evidence instead of deleting it."""
from pathlib import Path
import logging
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def main():
    evidence = ROOT / ".validation"
    evidence.mkdir(exist_ok=True)

    class RetainedTemporaryDirectory:
        def __init__(self, *args, **kwargs):
            self.name = tempfile.mkdtemp(prefix="case_", dir=str(evidence))
        def __enter__(self):
            return self.name
        def __exit__(self, *args):
            return False
        def cleanup(self):
            pass

    tempfile.TemporaryDirectory = RetainedTemporaryDirectory
    tempfile.tempdir = str(evidence)
    from yihuan.ui import PrivacyFormatter
    handler = logging.FileHandler(evidence / "simulated-events.log", encoding="utf-8")
    handler.setFormatter(PrivacyFormatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("yihuan")
    logger.addHandler(handler)
    logger.propagate = False
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    path = evidence / "unittest.log"
    with path.open("w", encoding="utf-8") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    print(f"Tests: {result.testsRun}; failures: {len(result.failures)}; errors: {len(result.errors)}")
    print(f"Evidence retained: {path}")
    if not result.wasSuccessful():
        for test, trace in result.failures + result.errors:
            print(test)
            print(trace)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
