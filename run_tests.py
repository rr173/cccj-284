#!/usr/bin/env python3
"""Run the test suite: ``python3 run_tests.py [-v]``."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"),
                            top_level_dir=os.path.dirname(os.path.abspath(__file__)))
    verbosity = 2 if "-v" in sys.argv else 1
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
