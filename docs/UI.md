# The Iron dashboard: `iron ui`

```bash
cd my-project
iron ui
```
```
Iron dashboard for my-project
  http://127.0.0.1:52817/?token=Xk3...
  Only this computer can open it, and only with that link. Press Ctrl+C to stop.
```

Your browser opens a page that shows the repository without any Git vocabulary.
Nothing is installed and nothing is downloaded: the page is built into Iron and
works offline. Press **Ctrl+C** in the terminal to stop it.

| Option | |
|---|---|
| `--port N` | listen on a fixed port (default: any free one) |
| `--no-browser` | print the link but do not open a browser |

## What you see

**At the top:** the repository's name, the line of work you are on (the
branch), the current version, and for each backup location whether it is
synced, has versions waiting to be published, or has new versions to
download (as of the last sync). **Sync now** downloads others' changes and
publishes yours, exactly like `iron sync`.

**Changes:** every file that differs from the last saved version (new,
changed, deleted, renamed); pick one to see what changed in it line by line
(added lines green, removed lines red). Type a description and press **Save version** to save everything as a
new version.

**History:** every saved version, newest first, with the person or AI agent
that made it. **Restore files to this version** puts every file back exactly as
it was. Your branch does not move: the restored files show up under Changes,
ready to save as a new version.

**Checkpoints:** snapshots of everything, including files not saved in a
version yet. Create one before a risky change and restore it if things go
wrong.

**Storage:** where each backup lives, whether it is reachable, and whether it
is encrypted. **Check backup** runs `iron verify`; **Full restore test** runs
`iron verify --deep`, which rebuilds the entire repository from the backup
alone. For an encrypted backup the page reminds you to keep the recovery key in
your password manager (it never shows the key itself; use `iron key show`).

**Agents:** work done by AI agents, grouped by session, from the `Iron-Agent`
and `Iron-Session` lines Iron records in every version and checkpoint.

## Nothing is lost by restoring

Every restore asks for confirmation first, and before it touches a file it
saves what you have now as a *safety checkpoint*. The page shows that
checkpoint's id afterwards, with an **Undo** button that restores it.

## Security

The dashboard can change your files, so it is locked down:

- It listens on `127.0.0.1` only; other computers cannot reach it.
- Each launch creates a new random token. Only the printed link carries it; the
  page swaps it for a cookie that scripts cannot read (`HttpOnly`) and other
  sites cannot send (`SameSite=Strict`), and removes it from the address bar.
- Requests whose `Host` is not `127.0.0.1` or `localhost` on the dashboard's
  port are refused, which stops DNS-rebinding attacks.
- Every change (save, checkpoint, restore, sync, verify) also needs the token
  in a request header and a same-origin `Origin`, so another web page cannot
  forge one (CSRF).
- The page runs under a strict Content-Security-Policy, loads nothing from the
  internet and cannot be embedded in another site.

Anyone who can run programs as you on this computer can already change your
files, so the dashboard does not try to defend against them.

## Long operations

Sync and the backup checks run in the background; the page shows a spinner and
stays usable. Only one change runs at a time; a second one is refused with
"please wait" until the first finishes. Errors appear as a sentence in the
page, never as a stack trace.
