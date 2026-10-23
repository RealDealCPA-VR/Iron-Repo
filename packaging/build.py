"""Builds standalone ``iron`` and ``git-remote-iron`` executables with PyInstaller.

    python -m pip install pyinstaller cryptography
    python packaging/build.py              # -> dist/standalone/iron(.exe), git-remote-iron(.exe)
    sh packaging/smoke.sh dist/standalone  # push and clone through the built executables

Each executable is a single file that needs no Python on the target computer, only git.
The ``cryptography`` package is bundled so encrypted remotes work too; the build refuses
to run without it. Put both files in the same folder: ``iron`` finds its sibling helper.
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / "dist" / "standalone"
WORK = ROOT / "build" / "pyinstaller"
TOOLS = [("iron", "iron_entry.py"), ("git-remote-iron", "git_remote_iron_entry.py")]


def main():
    try:
        import cryptography  # noqa: F401
        import PyInstaller  # noqa: F401
    except ImportError as e:
        sys.exit("build.py needs PyInstaller and cryptography: python -m pip install pyinstaller cryptography (%s)" % e)
    for name, entry in TOOLS:
        subprocess.check_call([
            sys.executable, "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean", "--console",
            "--name", name, "--distpath", str(OUT), "--workpath", str(WORK / name), "--specpath", str(WORK),
            "--paths", str(ROOT),
            "--collect-submodules", "iron",
            "--collect-submodules", "cryptography",
            # Every remote gets a copy of recover.py's source, so ship it beside the compiled module.
            "--add-data", "%s%siron" % (ROOT / "iron" / "recover.py", os.pathsep),
            str(HERE / entry)])
    for p in sorted(OUT.iterdir()):
        print("%s  %.1f MB" % (p, p.stat().st_size / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
