# Automatic saving and syncing

Iron can save and publish your work without you remembering `iron commit`
and `iron sync`. There are two independent pieces:

| Command | What it does |
|---|---|
| `iron autosave` | Every few minutes: if anything changed, save a version, then sync. |
| `iron autosync on` | After every commit you make: sync in the background. |

Both need an Iron remote. Without one they refuse and tell you how to add one
(`iron setup --to onedrive`, or `iron remote add origin D:/Backups/app.iron`).

## `iron autosave`

```
iron autosave                 # foreground, every 5 minutes; Ctrl+C stops it
iron autosave --every 30s     # 30s, 5m, 1h, or a number of seconds (at least 5)
iron autosave --quiet         # only print when something needs your attention
iron autosave --once          # one save-and-sync, then exit (scripts, CI, tests)
```

Each tick:

1. **Checks that nobody else is at work.** While `iron agent` runs here, the
   tick only logs "an agent is working; waiting": the agent's half-done work
   is never saved or published, so a rollback stays rolled back. The tick then
   takes the repository lock (`.git/iron/repo.lock`, shared by `iron sync`,
   `iron commit`-style saves, checkpoints and restores); if another Iron
   operation holds it, the tick is skipped and names its process. If a merge,
   rebase, cherry-pick or revert is in progress, or another git command holds
   `index.lock`, the tick is skipped and retried next time. A detached HEAD is
   skipped too.
2. **Looks for changes** with `git status --porcelain` (cheap; nothing is
   staged unless something changed).
3. **Saves a version** of every change, new files included, with the message
   `Autosave: <n> file(s) changed` and the trailer `Iron-Autosave: true`
   (plus `Iron-Agent`/`Iron-Session` when those are set). Your git hooks run
   as usual.
4. **Syncs** with the default remote exactly like `iron sync`: pull, combine,
   publish. This also runs when nothing changed, so versions saved while you
   were offline, and other computers' work, still flow.

When a sync fails, autosave reads what it said:

| What happened | What autosave does |
|---|---|
| Someone else published at the same moment (rejected, non-fast-forward) | retries at once, twice, then next tick |
| You edited files while it synced ("uncommitted changes") | tries again next tick |
| Another Iron operation was running | tries again next tick |
| Both sides changed the same lines | stops (see below) |
| Storage or connection error | "offline": the version is saved locally; the sync is retried next tick |

**Conflicts** (git holds files that both sides changed): autosave stops, and
says so loudly on every later tick until they are fixed: "Files conflict
between your computers: <files>". `iron`, `iron autosave --status` and the
dashboard show the same banner. Fix the files, `iron commit -am "Combine
changes"`, `iron sync`, and start it again. To back out: `git merge --abort`.
A merge that is merely in progress, with no conflicted files, is skipped
quietly instead.

Merges made by a sync are titled "Combine changes from <remote>". The
checkpoints Iron takes before syncing or merging are pruned to the newest 20;
your own, agent and restore-safety checkpoints are kept.

### Safety

- **Secrets are never saved.** Files that look like secrets (`.env`, `*.pem`,
  `*.key`, `id_rsa`, `credentials.json`, ... and any file containing
  `PRIVATE KEY-----`) are left out of every autosave, with a warning telling
  you to add them to `.gitignore`. The rest of your work is still saved. If
  such a file is already staged by hand, autosave refuses to save at all until
  you unstage it (`git restore --staged <file>`). The same rule applies to
  every version Iron saves: the dashboard's "Save version", `iron agent` and
  the Python API. `iron` lists them as "N file(s) kept out because they look
  like secrets" instead of counting them as pending changes.
- **Nothing is forced.** Autosave uses the normal sync: fast-forward or merge,
  never force-push, never delete.
- One autosave per repository: a second one refuses to start while the first
  is alive. If the first one's process has exited, the new one starts at once.
- **No interleaving.** Every Iron command that changes the repository holds
  `.git/iron/repo.lock` (pid and start time inside; a lock whose process is
  gone is taken over). Commands wait for it with "Waiting for another Iron
  operation to finish (pid N)"; `IRON_REPO_LOCK_TIMEOUT` (seconds, default
  300) sets how long before they give up with "another Iron operation is
  running (pid N)". Autosave never waits; it tries again next tick.

## Running at login: `--install`

```
iron autosave --install [--every 5m]   # start now and at every login
iron autosave --status                 # installed? running? last result? log file?
iron autosave --uninstall              # stop running at login (and stop the running one)
```

Per user, no admin rights:

| OS | How it is registered |
|---|---|
| Windows | a `.cmd` in your Startup folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\iron-autosave-<repo>.cmd`) that starts `pythonw` with no window |
| macOS | a launchd agent `~/Library/LaunchAgents/com.ironrepo.autosave.<repo>.plist` (`RunAtLoad`), loaded with `launchctl bootstrap gui/<uid>` |
| Linux | a systemd user service `~/.config/systemd/user/iron-autosave-<repo>.service`, `systemctl --user enable --now`; without a systemd user session, a `@reboot` line in your crontab (runs at boot rather than at login) |

Each registration starts a small runner script,
`<IRON_HOME>/autosave/<repo>-runner.py`, which remembers the repository, the
interval and where Iron is installed. It puts only the folders it needs (git's,
Iron's launchers' and Python's) in front of the login session's own `PATH`, so
later changes to your `PATH` still reach it.
`<repo>` is the folder name plus a short hash of its path, so two projects
with the same name do not clash.

**Logs** go to `<IRON_HOME>/logs/autosave-<repo>.log` (rotated at 1 MB, three
old copies kept). `IRON_HOME` defaults to `~/.iron`.

`--uninstall` removes the registration and the runner, and asks a running
autosave to stop; it exits within about 20 seconds.

**Dry run.** `IRON_AUTOSAVE_DRY_RUN=1 iron autosave --install` prints every
file it would write and every command it would run, and changes nothing.
`IRON_AUTOSAVE_PLATFORM=windows|macos|linux|linux-systemd|linux-cron` shows
what another platform would get.

## `iron autosync on|off`

```
iron autosync on      # install the post-commit hook
iron autosync off     # remove Iron's part of it
iron autosync         # is it on?
```

The hook starts `iron sync` in the background and returns at once, so the
commit never waits and can never fail because of it. Output goes to
`<IRON_HOME>/logs/autosync-<repo>.log`. While a sync is running, further
commits are queued and published by the same sync.

- Works with Windows Git Bash and any POSIX `sh`.
- Respects `core.hooksPath`.
- **Existing hooks keep working.** In a shell hook, Iron inserts one marked
  block right after the `#!` line (before any `exit`). A hook in another
  language (Python, Node, ...) is moved to `post-commit.iron-chained` and run
  from Iron's hook. `iron autosync off` removes only Iron's block, or puts the
  original hook back, byte for byte.
- Commits made by `iron autosave` do not trigger the hook (autosave syncs
  itself), and the hook does nothing during a merge, rebase or cherry-pick,
  or while `iron agent` is working. It takes the repository lock for its sync.

You can use both: autosave for unsaved edits, autosync for the commits you
make yourself.
