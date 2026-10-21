#!/bin/sh
# Iron Repository installer for macOS and Linux.
#
#   sh install.sh                    # basic
#   sh install.sh --with-encryption  # + optional encryption package
#
# Nothing is downloaded for the basic install: it adds two tiny launchers to
# ~/.local/bin that run Iron straight from this folder. Move the folder? Run it again.
set -e
here=$(cd "$(dirname "$0")" && pwd)
bin="$here/bin"
chmod +x "$bin/iron" "$bin/git-remote-iron"

command -v git >/dev/null 2>&1 || { echo "Iron needs Git: https://git-scm.com/downloads"; exit 1; }
py=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    py=$candidate
    break
  fi
done
[ -n "$py" ] || { echo "Iron needs Python 3.8 or newer: https://www.python.org/downloads/"; exit 1; }

target="$HOME/.local/bin"
mkdir -p "$target"
for tool in iron git-remote-iron; do
  printf '#!/bin/sh\nexec "%s/%s" "$@"\n' "$bin" "$tool" > "$target/$tool"
  chmod +x "$target/$tool"
done
echo "Installed iron and git-remote-iron into $target"

if [ "$1" = "--with-encryption" ]; then
  "$py" -m pip install --user --quiet cryptography ||
    echo "Could not install 'cryptography' with pip; install it with your package manager (e.g. python3-cryptography)."
fi

case ":$PATH:" in
  *":$target:"*) ;;
  *) echo "Add this line to your ~/.profile (or ~/.zshrc), then open a new terminal:"
     echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
     PATH="$target:$PATH" ;;
esac

"$target/iron" doctor || true
echo
echo "Next: go to a project folder and run:   iron setup"
