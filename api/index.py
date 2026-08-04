import sys, os
# Ensure the project root is on the path so `brain` package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain.wrappers.api import app
