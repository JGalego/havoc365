# Agent instructions

havoc365 is a Python CLI that maps a Microsoft 365 tenant's dependencies and runs outage drills. The README is the spec: its commands, sample output, permissions and safeguards are promises to users.

## Code

- Make the smallest change that does the job. Don't add abstractions, options or dependencies nobody has asked for, and explain any new dependency in the commit body.
- Use uv for everything (`uv run`, `uv add`). Code must run on Python 3.12.
- Comments explain why. Don't narrate what the code does or what you changed, and delete dead or commented-out code.
- Error messages say what failed and what to do next, such as the missing permission or role.
- havoc365 talks only to Microsoft Graph and DNS. Its only tenant writes are the drill's Conditional Access policy and session revocation. Ask before adding writes, Graph scopes or other network calls.
- Graph fails in the ways this tool prepares people for. Handle 429 (honour `Retry-After`), 5xx, timeouts and paging. If a drill can't revert, retry, then tell the user exactly what's still in the tenant.
- Never start a drill, with `drill run` or a live test, unless the user asks you to in the current conversation.

## Tests

Don't call a change done until you've run it. Say what you ran and what you couldn't run.

- Every code change comes with a test that fails without it.
- `uv run ruff check`, `uv run ruff format --check` and `uv run pytest` pass before every commit.
- Build fixtures from real Graph responses with tenant IDs, names and addresses scrubbed. Don't write responses you haven't seen.
- Test the failures users hit during an outage: throttling, 5xx, timeouts, missing permissions, no Entra ID P1, and a drill process killed before it reverts.
- Live tests run against a dedicated test tenant, never production. Mark them `@pytest.mark.live` and skip them unless `HAVOC365_TEST_TENANT` is set. Run them after changing code that calls Graph.
- Run the command you changed the way a user would, against the test tenant when you have one.
- A test checks that the README's permissions table matches the scopes the code requests.

## Docs

- Follow [WRITING.md](WRITING.md).
- Update the README in the same commit as the behaviour it describes, including sample output.
- Don't create summary, plan or notes files unless asked.
- The logo comes from `assets/logo.py`. Edit the script and run `uv run assets/logo.py`; don't edit the images.

## Commits

- Follow [CONTRIBUTING.md](CONTRIBUTING.md), with a `Co-authored-by` trailer for yourself.
- Keep one logical change per commit. Don't push or add remotes unless asked.
- Never commit secrets, tokens, tenant IDs or unscrubbed Graph data.
