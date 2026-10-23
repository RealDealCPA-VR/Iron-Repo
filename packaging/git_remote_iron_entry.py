"""Entry point of the standalone git-remote-iron executable (see packaging/build.py)."""
import sys

from iron.remote_helper import main

sys.exit(main())
