# Platforms

Iron is tested on Windows, macOS and Linux on every push, and can be built into
standalone `iron` and `git-remote-iron` executables that need no Python on the
target computer, only git.

## What CI covers

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push and pull request:

| Runner | Python | Steps |
|---|---|---|
| `ubuntu-latest` | 3.8, 3.13 | full test suite, launcher smoke test, `install.sh` smoke test |
| `macos-latest` (arm64) | 3.9, 3.13 | the same (no Python 3.8 build exists for arm64 macOS runners) |
| `windows-latest` | 3.8, 3.13 | the same, with Git Bash as the shell |

Each job:

1. installs `pytest` and `cryptography` and runs `python -m pytest -q -p no:asyncio`;
2. puts `bin/` on PATH and runs [`packaging/smoke.sh`](../packaging/smoke.sh):
   `iron --version`, `iron doctor`, then **plain git** pushes to and clones from
   a folder remote through `iron::`, plus the same through an encrypted remote
   created with `iron remote add --encrypt`. Everything happens in a fresh temp
   folder with its own `IRON_HOME` and git config;
3. runs `sh install.sh` with `HOME` set to a temp folder and checks that the
   installed `~/.local/bin/iron` runs.

[`.github/workflows/build.yml`](../.github/workflows/build.yml) runs on pushes to
`ci/portability` and on demand (workflow_dispatch). It builds the standalone
executables and smoke-tests them:

| Target | Runner | Extra check |
|---|---|---|
| `windows-x64` | `windows-latest` | |
| `macos-arm64` | `macos-latest` | |
| `linux-x64` | `ubuntu-22.04` (glibc 2.35, so the binary also runs on older distributions) | the smoke test runs again in a `debian:stable-slim` container that has git and **no Python at all** |

The executables are uploaded as workflow artifacts `iron-windows-x64`
(`iron.exe`, `git-remote-iron.exe`), `iron-macos-arm64` and `iron-linux-x64`
(each a `.tar.gz`, because artifact zips drop the Unix executable bit). No
releases or tags are created.

## Building the executables yourself

```bash
python -m pip install pyinstaller cryptography
python packaging/build.py               # -> dist/standalone/iron(.exe), git-remote-iron(.exe)
sh packaging/smoke.sh dist/standalone   # push and clone through the built executables
```

[`packaging/build.py`](../packaging/build.py) makes two PyInstaller `--onefile`
executables from [`packaging/iron_entry.py`](../packaging/iron_entry.py) and
[`packaging/git_remote_iron_entry.py`](../packaging/git_remote_iron_entry.py).
It bundles every `iron` module and the `cryptography` package (it refuses to
build without it, so encryption always works), and ships `iron/recover.py` as a
data file because every remote gets a copy of its source. Build on each
platform you want to ship: PyInstaller does not cross-compile.

To install, put both files in one folder on PATH (on macOS/Linux, unpack the
tarball; it keeps the executable bit). `iron` also finds a `git-remote-iron`
sitting next to it even when that folder is not on PATH, and `iron doctor`
reports it. On macOS, a downloaded binary is quarantined by Gatekeeper; run
`xattr -d com.apple.quarantine iron git-remote-iron` once, or build it yourself.

## Results

All green on the final runs of branch `ci/portability`: 6/6 CI jobs and 3/3
build jobs. Standalone sizes: about 12 MB per executable on macOS arm64, 13 MB
on Windows x64 and 26 MB on Linux x64.

Portability bugs found by running on macOS and Linux for the first time, and fixed:

- **Push lock could admit two holders** (seen on Linux, where file operations
  are fast). Lock files are named `<ms>-<random>.lock` and the first in sort
  order wins; a second device that wrote its lock in the same millisecond as
  the current holder could sort first on its random suffix and also believe it
  held the lock. A same-millisecond tie now counts as busy for the newcomer.
  (The push log protocol already detected the race; the lock is now meaningful
  again.)
- **`test_resolve_location` assumed Windows paths.** `C:/x/y.iron` is a
  relative path on POSIX, which Iron correctly made absolute; the test now uses
  a path that is absolute on the platform it runs on.

Changes for the standalone build:

- `gitutil.helper_dirs()` also looks next to `sys.executable` when running
  frozen, so a sibling `git-remote-iron(.exe)` is found even when `iron` was
  started by a bare name through PATH.
- The `iron-recover.py` written into remotes is read through
  `remote.recover_source()`, which reads `recover.py` beside the compiled
  module (the frozen build has only `recover.pyc` in its archive).
- `iron doctor` says when its Python is the one built into the executable.

Checked and found fine: the `sh` launchers (LF endings are enforced by
`.gitattributes`), `os.link` exclusive creation on APFS and ext4, macOS
`/var` → `/private/var` temp paths, Python 3.8 syntax and stdlib use
(`shutil.rmtree(onexc=...)` is already version-gated), and `interactive()`
(stdin is not a tty under CI and git, so no prompt blocks).

Not covered: Linux on arm64, Intel macOS, and Windows on arm64 have no
standalone build yet; real cloud providers (rclone) are exercised only through
the fake rclone in the tests.
