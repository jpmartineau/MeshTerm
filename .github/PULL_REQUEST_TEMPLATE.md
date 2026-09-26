## What this changes, and why

<!-- The why matters more than the what — the diff already says the what. -->

## Related issue

<!-- Small fixes can come straight to a PR. For anything bigger, CONTRIBUTING asks for an
     issue first and a yes on it, so a large PR with no issue behind it will get a
     conversation before it gets a review. Link it here. -->

## Before you mark it ready

- [ ] I've read [CONTRIBUTING.md](../blob/main/CONTRIBUTING.md) and agree its licensing terms apply to this contribution
- [ ] `python -m pytest -q` passes, including the dual-platform gallery gate
- [ ] `ruff check .` and `ruff format --check .` are clean
- [ ] New screens, rows, and hints go through the helpers in `ui/menus.py`, and follow the
      lexicon and UX standards in `CLAUDE.md`
- [ ] Anything user-visible has a line in `CHANGELOG.md` under `[Unreleased]`
