# Iron for coding agents

Agents (Claude, Codex, GLM, local models) can change a lot of files quickly.
Iron makes every meaningful change versioned and every mistake reversible.

## The guarded run

```bash
iron agent --name Claude --test "pytest -q" -- claude -p "add retry handling to the OneDrive adapter"
```

1. **Checkpoint.** Iron snapshots the whole working tree, including untracked
   files such as `.env` and generated configs, but not ignored ones. Nothing
   in your working tree or staging area changes.
2. **Run.** The agent runs with `IRON_AGENT`, `IRON_SESSION` and
   `IRON_CHECKPOINT` in its environment.
3. **Validate.** Your `--test` command runs.
4. **Commit or roll back.**
   - If the tests pass, Iron commits everything with `Iron-Agent: Claude` and
     `Iron-Session: <id>` trailers. `--push` also syncs.
   - If they fail (or the agent exits non-zero), the files are restored to
     the checkpoint, and the branch is moved back to where it was, which
     undoes any commits the agent made. The agent's attempt is saved as a
     checkpoint too, so you can still look at it (`iron checkpoints`,
     `git diff refs/iron/checkpoints/<id>`). `--keep` leaves the failed
     changes in place instead.
   - The agent never sees `IRON_KEY`.

`iron history` then shows who did what:

```
3f1c2a9  Claude       Add storage abstraction (12 minutes ago)
8d02e77  Codex        Fix OneDrive retry behavior (27 minutes ago)
51b9c40  GLM          Add provider tests (44 minutes ago)
```

## Manual control

```bash
iron checkpoint -m "before refactor"      # prints the checkpoint id
iron restore last                         # or an id, an id prefix, or any commit
iron checkpoints
iron merge feature-x                      # saves a checkpoint before merging
```

`iron restore` always saves the current state first, so a restore can itself
be undone.

## Python API

```python
from iron.api import Repository

repo = Repository(".", agent="Claude", session="task-42")
cp = repo.checkpoint("before task")
# ... the agent edits files ...
print(repo.diff())            # unified diff, including new files
if tests_pass:
    repo.commit("Add storage abstraction")
    repo.push()
else:
    repo.restore(cp["id"])
repo.status(); repo.history(10); repo.verify(deep=True)
```

Every method returns plain dicts, lists or strings, ready for JSON. The CLI
equivalents take `--json` (`iron checkpoint --json`, `iron checkpoints
--json`, `iron history --json`, `iron verify --json`, `iron restore <id>
--json`).

## Claude Code hooks

To checkpoint automatically every time Claude Code starts a task, add this to
`.claude/settings.json` in the project:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "iron checkpoint -m \"before Claude task\" --agent Claude" } ] }
    ]
  }
}
```

## Safety defaults (PRD section 16)

| Protection | How |
|---|---|
| No force push by default | the remote helper refuses non-fast-forward updates unless `iron.allowForcePush` is set |
| No history deletion | nothing in a remote is ever deleted; even a forced update keeps the old commits in the log |
| No remote branch deletion without confirmation | `git push --delete` is refused; `iron push --delete` asks first |
| Automatic checkpoint before agent work | `iron agent`, or the hook above |
| Automatic backup before merge | `iron merge`, and `iron sync` before combining |
| Immutable commit objects | Git objects plus write-once storage |
| Hash validation on every download | SHA-256 of every bundle and record, plus git's own object checks |
