#!/bin/sh
# End-to-end smoke test of an Iron install, for CI and for checking a standalone build.
#
#   sh packaging/smoke.sh <folder holding iron and git-remote-iron>
#
# Plain git pushes to and clones from a folder remote through iron::, then does the
# same with an encrypted remote. Everything happens in a fresh temp folder with its own
# Iron home and git config; nothing outside it is touched.
set -eu
bindir=$(cd "$1" && pwd)
PATH="$bindir:$PATH"
export PATH

work=$(mktemp -d)
# Git for Windows' sh sees /tmp/...; Python and git-remote-iron need a C:/... path.
if command -v cygpath >/dev/null 2>&1; then work=$(cygpath -m "$work"); fi
export IRON_HOME="$work/iron-home"
export GIT_CONFIG_GLOBAL="$work/gitconfig"
export GIT_CONFIG_NOSYSTEM=1
git config --global user.name "Smoke Test"
git config --global user.email "smoke@example.com"
git config --global init.defaultBranch main
git config --global core.autocrlf false

echo "== iron --version"
iron --version </dev/null

echo "== iron doctor"
iron doctor </dev/null || true  # informational: optional items (rclone) are missing on runners

git init -q "$work/a"
echo "hello from $(uname -s)" > "$work/a/README.md"
git -C "$work/a" add README.md
git -C "$work/a" commit -q -m "first"

echo "== git push / git clone through iron::"
git -C "$work/a" remote add origin "iron::$work/remote.iron"
IRON_CREATE=1 git -C "$work/a" push -q origin main </dev/null  # plain git may create the remote
git clone -q "iron::$work/remote.iron" "$work/b" </dev/null
cmp "$work/a/README.md" "$work/b/README.md"
test -f "$work/remote.iron/iron-recover.py"

echo "== encrypted remote"
(cd "$work/a" && iron remote add secret "$work/secret.iron" --encrypt </dev/null)
git -C "$work/a" push -q secret main </dev/null
grep -q "aes-256-gcm" "$work/secret.iron/iron.json"
git clone -q "iron::$work/secret.iron" "$work/c" </dev/null
cmp "$work/a/README.md" "$work/c/README.md"

rm -rf "$work"
echo "== smoke test passed"
