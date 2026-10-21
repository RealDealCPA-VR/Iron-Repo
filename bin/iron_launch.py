"""Runs Iron straight from this folder, with no installation step."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
which = sys.argv.pop(1)
if which == "helper":
    from iron.remote_helper import main
else:
    from iron.cli import main
sys.exit(main())
