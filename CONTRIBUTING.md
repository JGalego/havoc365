# Contributing

## Commit messages

Start each commit message with a type and a summary:

```text
<type>: <summary>
```

| Type | Use it for |
|---|---|
| `feat` | A new command, option or check |
| `fix` | A bug fix |
| `docs` | The README, guides and other documentation |
| `test` | Tests only |
| `refactor` | Code changes that don't change behaviour |
| `chore` | Dependencies, build, CI and repository housekeeping |

Write the summary in the imperative ("add", not "added"), keep the line under 72 characters and leave off the full stop. If the reason for the change isn't obvious from the summary, explain it in the body.

## AI co-authors

If an AI tool wrote or changed any part of a commit, end the message with a `Co-authored-by` trailer for each tool:

```text
docs: add README, logo, license and commit policy

Co-authored-by: Claude Opus 5.5 <noreply@anthropic.com>
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```
