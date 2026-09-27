## What this changes, and why

<!-- The why matters more than the what — the diff already says the what. -->

## Discord

<!-- Your Discord username on the MeshTerm server, and a link to the message in
     #contributing where you got a yes. A pull request without them is closed without
     a review. -->

Username:
Link to the yes:

## AI use

<!-- Required. Did an AI tool write any part of this change? If yes: which tool, what it
     did, and what you checked yourself. Each commit it helped with also needs a line at
     the end naming it, like `Co-Authored-By:` or `Assisted-by:`. If you used AI and don't
     say so, this pull request is treated as a bot's and closed. -->

## Before you mark it ready

- [ ] I've read [CONTRIBUTING.md](../blob/main/CONTRIBUTING.md) and agree its licensing terms apply to this contribution
- [ ] I've put my Discord username and a link to the yes above
- [ ] If I used AI, I said so above and in the commits it helped with
- [ ] `python -m pytest -q` passes, including the dual-platform gallery gate
- [ ] `ruff check .` and `ruff format --check .` are clean
- [ ] New screens, rows, and hints go through the helpers in `ui/menus.py`, and follow the
      lexicon and UX standards in `CLAUDE.md`
- [ ] Anything user-visible has a line in `CHANGELOG.md` under `[Unreleased]`
