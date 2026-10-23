"""A stand-in for the rclone binary, backed by a local folder, so the rclone
provider can be tested without network access. Implements the subcommands and
exit codes Iron uses. Remotes look like  fake:some/path  ->  $FAKE_RCLONE_ROOT/some/path

Remotes made with `config create <name> <type>` are written to $RCLONE_CONFIG (an
INI file, like real rclone; default $FAKE_RCLONE_ROOT/rclone.conf) and map to
$FAKE_RCLONE_ROOT/<name>/... No browser is opened and nothing leaves the computer.
"""
import configparser
import os
import shutil
import sys


def config_path():
    return os.environ.get("RCLONE_CONFIG") or os.path.join(os.environ["FAKE_RCLONE_ROOT"], "rclone.conf")


def load_config():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(config_path(), encoding="utf-8")
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(os.path.abspath(config_path())), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as f:
        cfg.write(f)


def resolve(spec):
    name, _, path = spec.partition(":")
    root = os.environ["FAKE_RCLONE_ROOT"]
    if name != "fake":
        if not load_config().has_section(name):
            sys.stderr.write("didn't find section in config file (%s)\n" % name)
            sys.exit(1)
        root = os.path.join(root, name)
    return os.path.join(root, *[p for p in path.split("/") if p])


def config(args):
    """config create <name> <type> [key=value ...] | config delete <name> | config file"""
    cfg = load_config()
    if args[:1] == ["create"] and len(args) >= 3:
        name, kind = args[1], args[2]
        if os.environ.get("FAKE_RCLONE_FAIL_CREATE"):
            sys.stderr.write("failed to get token: user cancelled\n")
            return 1
        if not cfg.has_section(name):
            cfg.add_section(name)
        cfg.set(name, "type", kind)
        for kv in args[3:]:
            if "=" in kv and not kv.startswith("-"):
                key, _, value = kv.partition("=")
                cfg.set(name, key, value)
        save_config(cfg)
        print("[%s]\ntype = %s" % (name, kind))
        return 0
    if args[:1] == ["delete"] and len(args) == 2:
        cfg.remove_section(args[1])
        save_config(cfg)
        return 0
    if args[:1] == ["file"]:
        print("Configuration file is stored at:\n%s" % config_path())
        return 0
    sys.stderr.write("unknown config command %s\n" % " ".join(args))
    return 1


def main(args):
    cmd = args[0]
    if cmd == "version":
        print("rclone v1.99.0-fake")
        return 0
    if cmd == "config":
        return config(args[1:])
    if cmd == "listremotes":
        cfg = load_config()
        long = "--long" in args
        print("fake:     local" if long else "fake:")
        for name in cfg.sections():
            print(("%s: %s" % (name, cfg.get(name, "type", fallback=""))) if long else name + ":")
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
    if cmd == "mkdir":
        os.makedirs(resolve(args[1]), exist_ok=True)
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
