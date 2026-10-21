"""A stand-in for the rclone binary, backed by a local folder, so the rclone
provider can be tested without network access. Implements the subcommands and
exit codes Iron uses. Remotes look like  fake:some/path  ->  $FAKE_RCLONE_ROOT/some/path
"""
import os
import shutil
import sys


def resolve(spec):
    name, _, path = spec.partition(":")
    if name != "fake":
        sys.stderr.write("didn't find section in config file (%s)\n" % name)
        sys.exit(1)
    return os.path.join(os.environ["FAKE_RCLONE_ROOT"], *[p for p in path.split("/") if p])


def main(args):
    cmd = args[0]
    if cmd == "listremotes":
        print("fake:     local")
        return 0
    if cmd == "cat":
        p = resolve(args[1])
        if not os.path.isfile(p):
            sys.stderr.write("error: object not found\n")
            return 3
        with open(p, "rb") as f:
            sys.stdout.buffer.write(f.read())
        return 0
    if cmd == "rcat":
        p = resolve(args[1])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(sys.stdin.buffer.read())
        return 0
    if cmd == "deletefile":
        p = resolve(args[1])
        if not os.path.isfile(p):
            sys.stderr.write("error: object not found\n")
            return 4
        os.remove(p)
        return 0
    if cmd == "rmdir":
        try:
            os.rmdir(resolve(args[1]))
        except OSError:
            return 1
        return 0
    if cmd == "lsf":
        files_only = "--files-only" in args
        p = resolve([a for a in args[1:] if ":" in a][0])
        if not os.path.isdir(p):
            sys.stderr.write("error: directory not found\n")
            return 3
        for n in sorted(os.listdir(p)):
            full = os.path.join(p, n)
            if os.path.isdir(full):
                if not files_only:
                    print(n + "/")
            else:
                print(n)
        return 0
    if cmd == "copy":
        src, dst = resolve(args[1]), args[2]
        listing = args[args.index("--files-from") + 1]
        with open(listing, encoding="utf-8") as f:
            for rel in [line.strip() for line in f if line.strip()]:
                s = os.path.join(src, *rel.split("/"))
                if os.path.isfile(s):
                    d = os.path.join(dst, *rel.split("/"))
                    os.makedirs(os.path.dirname(d), exist_ok=True)
                    shutil.copyfile(s, d)
        return 0
    sys.stderr.write("unknown command %s\n" % cmd)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
