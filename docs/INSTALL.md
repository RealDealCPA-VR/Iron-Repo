# Installing and updating Iron

## One line

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.ps1 | iex
```

**macOS / Linux**:

```bash
curl -fsSL https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.sh | sh
```

The installer:

1. Checks for Git and Python 3.8+. If one is missing, it tells you the exact command that
   installs it and asks before running it (see below).
2. Gets Iron. It uses `git clone --depth 1` when Git is available, or downloads the GitHub
   archive otherwise. Iron goes into a per-user folder:
   - Windows: `%LOCALAPPDATA%\Iron`
   - macOS / Linux: `~/.local/share/iron`
3. Puts `iron` on your PATH. On Windows it adds `...\Iron\bin` to your user PATH. On macOS
   and Linux it writes two small launchers to `~/.local/bin` and, if that folder is not on
   your PATH yet, prints the line to add to `~/.profile`.
4. Runs `iron doctor`.

If Iron is already installed, running the one-liner again updates it (same as `iron update`).

### Options

| | Windows | macOS / Linux |
|---|---|---|
| Accept the offer to install Git / Python | `-Yes` | `--yes` |
| Also install the optional encryption package | `-WithEncryption` | `--with-encryption` |

To pass options to the one-liners:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.ps1))) -Yes -WithEncryption
```

```bash
curl -fsSL https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.sh | sh -s -- --yes --with-encryption
```

## Missing Git or Python

The installer never installs anything without asking. When Git or Python is missing, it
shows the command and asks `[y/N]`. If nobody is there to answer (a script, CI), the answer
is **no**. In that case it prints the command so you can run it yourself, unless you passed
`-Yes` / `--yes`.

| System | Git | Python |
|---|---|---|
| Windows | `winget install --id Git.Git -e` | `winget install --id Python.Python.3.12 -e` |
| macOS (with Homebrew) | `brew install git` | `brew install python` |
| Debian / Ubuntu | `sudo apt-get install -y git` | `sudo apt-get install -y python3` |
| Fedora / RHEL | `sudo dnf install -y git` | `sudo dnf install -y python3` |
| Arch | `sudo pacman -S --needed git` | `sudo pacman -S --needed python` |
| openSUSE | `sudo zypper install -y git` | `sudo zypper install -y python3` |

Without a package manager (for example macOS without Homebrew), the installer prints the
download links: <https://git-scm.com/downloads> and <https://www.python.org/downloads/>.
On Linux, `sudo` is used only when you are not root, and only after you said yes.

`iron doctor` gives the same one-line fixes for anything it finds missing.

## From a clone

If you would rather keep Iron in a folder you cloned yourself, run the installer from that
folder. Iron then runs straight from it. If you move the folder, run the installer again.

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

```bash
sh install.sh
```

Prefer Python packaging? `pipx install .` works too. Update that copy with pipx.

## Updating

```
iron update
```

- **Git checkout** (the default when Git was present at install time): runs
  `git pull --ff-only` in the install folder.
- **Downloaded copy**: downloads the archive again into a temporary folder next to the
  install. The new copy must pass `iron --version` before it is swapped in. The old copy is
  kept until the new one works in its final place, and it is put back if anything fails.

Either way, Iron prints the old and the new version, for example `Updated Iron 0.1.0 -> 0.2.0`.

## Uninstalling

Delete the install folder (`%LOCALAPPDATA%\Iron` or `~/.local/share/iron`). On macOS and
Linux, also delete `~/.local/bin/iron` and `~/.local/bin/git-remote-iron`. On Windows,
remove `...\Iron\bin` from your user PATH (Settings > System > About > Advanced system
settings > Environment Variables). Your repositories, remotes and keys (`~/.iron`) are not
touched.

## Environment variables

Mostly useful for testing, mirrors, or air-gapped installs:

| Variable | Effect |
|---|---|
| `IRON_INSTALL_DIR` | install somewhere else |
| `IRON_INSTALL_URL` | a git URL to clone, or a `.zip` (Windows) / `.tar.gz` or `.zip` (macOS, Linux) to download, instead of GitHub. `file://` URLs work. `iron update` uses it too, and otherwise reuses the URL a downloaded copy came from. |
| `IRON_INSTALL_YES=1` | same as `-Yes` / `--yes` |
| `IRON_INSTALL_NO_PATH=1` | do not change PATH (Windows) or write `~/.local/bin` launchers (macOS, Linux) |
| `IRON_INSTALL_DRY_RUN=1` | print the Git / Python install commands instead of running them |
