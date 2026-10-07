## What this changes, and why

<!-- The reason is more important than the description of the change, because the diff already shows the change. -->

## Discord

<!-- Write your Discord username on the MeshTerm server. Also write a link to the message
     in #contributing where you got a yes. We close a pull request without these items
     and do not review it. -->

Username:
Link to the yes:

## AI use

<!-- This is necessary. Did an AI tool write any part of this change? If yes, write which
     tool, what it did, and what you checked yourself. Each commit that it helped with also
     needs a line at the end that names the tool, for example `Co-Authored-By:` or
     `Assisted-by:`. If you used AI and you do not say so, we treat this pull request as a
     bot's pull request and close it. -->

## Before you mark it ready

- [ ] I have read [CONTRIBUTING.md](../blob/main/CONTRIBUTING.md) and I agree that its licensing terms apply to this contribution
- [ ] I have written my Discord username and a link to the yes above
- [ ] If I used AI, I said so above and in the commits that it helped with
- [ ] `python -m pytest -q` passes, including the dual-platform gallery gate
- [ ] `ruff check .` and `ruff format --check .` have no errors
- [ ] New screens, rows, and hints use the helpers in `ui/menus.py`, and follow the
      lexicon and UX standards in `CLAUDE.md`
- [ ] Anything that the user can see has a line in `CHANGELOG.md` under `[Unreleased]`
