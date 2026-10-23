"""Entry point of the standalone iron executable (see packaging/build.py)."""
import sys

from iron.cli import main

sys.exit(main())
