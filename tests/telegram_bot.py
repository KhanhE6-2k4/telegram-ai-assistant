"""Chạy test trong môi trường của Briefcase: briefcase dev --test (hoặc briefcase run --test)."""
import os
import sys
from pathlib import Path

import pytest


def run_tests():
    project_path = Path(__file__).parent.parent
    os.chdir(project_path)
    returncode = pytest.main(["-vv", "--no-header", "-p", "no:cacheprovider", str(project_path / "tests")])
    print(f">>>>>>>>>> EXIT {returncode} <<<<<<<<<<")
    sys.exit(returncode)


if __name__ == "__main__":
    run_tests()
