#!/bin/sh
# Iron Repository installer for macOS and Linux.
#
# One line, from anywhere (installs into ~/.local/share/iron):
#   curl -fsSL https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.sh | sh -s -- --yes --with-encryption
# From a folder you cloned (Iron runs straight from that folder; move it? run this again):
#   sh install.sh [--with-encryption] [--yes]
#
# Either way it adds two tiny launchers to ~/.local/bin. --yes accepts the offer to
# install a missing Git or Python with your package manager. Nothing is ever installed
# without that, or without you answering "y".
#
# Environment (mostly for testing):
#   IRON_INSTALL_DIR        where to install (default ~/.local/share/iron)
#   IRON_INSTALL_URL        a git URL to clone, or a .tar.gz/.zip to download, instead of GitHub
#   IRON_INSTALL_YES=1      same as --yes
#   IRON_INSTALL_NO_PATH=1  do not write launchers into ~/.local/bin
#   IRON_INSTALL_DRY_RUN=1  print package-manager commands instead of running them
set -e

REPO_URL=https://github.com/RealDealCPA-VR/Iron-Repo.git
TARBALL_URL=https://github.com/RealDealCPA-VR/Iron-Repo/archive/refs/heads/main.tar.gz

parse_args() {
  yes=""
  encryption=""
  for arg in "$@"; do
    case "$arg" in
      -y|--yes) yes=1 ;;
      --with-encryption) encryption=1 ;;
      *) echo "install.sh: unknown option $arg (use --yes or --with-encryption)" >&2; exit 2 ;;
    esac
  done
  truthy "${IRON_INSTALL_YES:-}" && yes=1
  dry_run=""
  if truthy "${IRON_INSTALL_DRY_RUN:-}" || [ -n "${IRON_INSTALL_PRETEND_MISSING:-}" ]; then dry_run=1; fi
}

truthy() { case "$1" in ""|0|false|no) return 1 ;; *) return 0 ;; esac; }
pretend_missing() { case ",${IRON_INSTALL_PRETEND_MISSING:-}," in *",$1,"*) return 0 ;; esac; return 1; }

confirm() {  # confirm QUESTION: true for --yes, or when a person at the terminal answers y
  if [ -n "$yes" ]; then return 0; fi
  if [ -t 1 ] && (exec </dev/tty) 2>/dev/null; then
    printf '%s [y/N] ' "$1"
    read -r answer </dev/tty || answer=""
    case "$answer" in y|Y|yes|Yes|YES) return 0 ;; esac
    return 1
  fi
  echo "$1 [y/N] no (nobody to ask; run again with --yes to accept)"
  return 1
}

find_python() {
  py=""
  pretend_missing python && return 0
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
      py=$candidate
      return 0
    fi
  done
}

have_git() { ! pretend_missing git && command -v git >/dev/null 2>&1; }

package_command() {  # package_command git|python: the command that installs it here, or nothing
  case "$(uname -s 2>/dev/null)" in
    Darwin)
      command -v brew >/dev/null 2>&1 && echo "brew install $1"
      return 0 ;;
    MINGW*|MSYS*|CYGWIN*)
      if command -v winget >/dev/null 2>&1; then
        if [ "$1" = python ]; then echo "winget install --id Python.Python.3.12 -e"; else echo "winget install --id Git.Git -e"; fi
      fi
      return 0 ;;
  esac
  sudo=""
  [ "$(id -u 2>/dev/null || echo 0)" = 0 ] || sudo="sudo "
  pkg=$1
  if command -v apt-get >/dev/null 2>&1; then
    [ "$1" = python ] && pkg=python3; echo "${sudo}apt-get install -y $pkg"
  elif command -v dnf >/dev/null 2>&1; then
    [ "$1" = python ] && pkg=python3; echo "${sudo}dnf install -y $pkg"
  elif command -v pacman >/dev/null 2>&1; then
    echo "${sudo}pacman -S --needed $pkg"
  elif command -v zypper >/dev/null 2>&1; then
    [ "$1" = python ] && pkg=python3; echo "${sudo}zypper install -y $pkg"
  elif command -v brew >/dev/null 2>&1; then
    echo "brew install $1"
  fi
}

offer_install() {  # offer_install git|python LABEL LINK: false unless it was installed
  echo "Iron needs $2, which is not installed."
  cmd=$(package_command "$1")
  if [ -z "$cmd" ]; then
    echo "Install it from $3, then run this installer again."
    return 1
  fi
  note=""
  case "$cmd" in sudo*) note=" (uses sudo, so it may ask for your password)" ;; esac
  if ! confirm "Install it now with:   $cmd   ?$note"; then
    echo "Not installing. Run it yourself:   $cmd   (or download from $3), then run this installer again."
    return 1
  fi
  if [ -n "$dry_run" ]; then echo "[dry run] would run: $cmd"; return 1; fi
  $cmd
}

download() {  # download URL FILE
  case "$1" in
    file://*)
      path=${1#file://}
      case "$path" in /[A-Za-z]:/*) path=${path#/} ;; esac
      cp "$path" "$2" ;;
    *)
      if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
      elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
      else "$py" -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2"
      fi ;;
  esac
}

fetch() {  # fetch DEST: put a fresh copy of Iron in DEST, which must not exist yet
  url=${IRON_INSTALL_URL:-}
  if [ -z "$url" ]; then
    if have_git; then url=$REPO_URL; else url=$TARBALL_URL; fi
  fi
  case "$url" in
    *.tar.gz|*.tgz|*.zip)
      work="$1.download.$$"
      rm -rf "$work"
      mkdir -p "$work/x"
      echo "Downloading $url"
      download "$url" "$work/archive"
      case "$url" in
        *.zip) "$py" -m zipfile -e "$work/archive" "$work/x" ;;
        *) tar -xzf - -C "$work/x" < "$work/archive" ;;
      esac
      top=""
      for d in "$work/x" "$work/x"/*; do
        if [ -f "$d/iron/cli.py" ]; then top=$d; break; fi
      done
      [ -n "$top" ] || { rm -rf "$work"; echo "$url does not contain Iron." >&2; exit 1; }
      mv "$top" "$1"
      rm -rf "$work"
      echo "$url" > "$1/.iron-source" ;;
    *)
      echo "Cloning $url"
      git clone --depth 1 --quiet "$url" "$1" ;;
  esac
}

install_remote() {  # sets root to a per-user copy of Iron, installing or updating it
  root=${IRON_INSTALL_DIR:-$HOME/.local/share/iron}
  if [ -e "$root/.git" ] || [ -f "$root/iron/cli.py" ]; then
    echo "Iron is already installed in $root; updating it."
    "$py" "$root/bin/iron_launch.py" cli update
  elif [ -d "$root" ] && [ -n "$(ls -A "$root")" ]; then
    echo "$root already exists and is not an Iron install. Set IRON_INSTALL_DIR to another folder." >&2
    exit 1
  else
    mkdir -p "$(dirname "$root")"
    rmdir "$root" 2>/dev/null || true
    fetch "$root"
    echo "Installed Iron into $root"
  fi
}

need_prerequisites() {
  find_python
  if [ -z "$py" ]; then
    offer_install python "Python 3.8 or newer" https://www.python.org/downloads/ || exit 1
    find_python
    [ -n "$py" ] || { echo "Python 3.8 or newer is still not found. Open a new terminal and run this again."; exit 1; }
  fi
  if ! have_git; then
    offer_install git Git https://git-scm.com/downloads || exit 1
    have_git || { echo "Git is still not found. Open a new terminal and run this again."; exit 1; }
  fi
}

add_launchers() {  # add_launchers BIN: two tiny scripts in ~/.local/bin that run Iron from BIN
  target="$HOME/.local/bin"
  if truthy "${IRON_INSTALL_NO_PATH:-}"; then
    echo "Not adding launchers to $target (IRON_INSTALL_NO_PATH). Iron is in $1"
    return 0
  fi
  mkdir -p "$target"
  for tool in iron git-remote-iron; do
    printf '#!/bin/sh\nexec "%s/%s" "$@"\n' "$1" "$tool" > "$target/$tool"
    chmod +x "$target/$tool"
  done
  echo "Installed iron and git-remote-iron into $target"
  case ":$PATH:" in
    *":$target:"*) ;;
    *) echo "Add this line to your ~/.profile (or ~/.zshrc), then open a new terminal:"
       echo "    export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac
}

# Everything runs from main, so with `curl ... | sh` the whole script has been read
# before any command that might read stdin starts.
main() {
  parse_args "$@"
  need_prerequisites
  root=""
  case "$0" in
    *install.sh)
      here=$(cd "$(dirname "$0")" && pwd)
      [ -f "$here/iron/cli.py" ] && root=$here ;;
  esac
  [ -n "$root" ] || install_remote </dev/null
  bin="$root/bin"
  chmod +x "$bin/iron" "$bin/git-remote-iron"
  add_launchers "$bin"
  if [ -n "$encryption" ]; then
    "$py" -m pip install --user --quiet cryptography </dev/null ||
      echo "Could not install 'cryptography' with pip; install it with your package manager (e.g. python3-cryptography)."
  fi
  PATH="$bin:$PATH" "$bin/iron" doctor </dev/null || true
  echo
  echo "Next: go to a project folder and run:   iron setup"
}

main "$@"
