# Contributing to MeshTerm

Thanks for wanting to work on MeshTerm. A few things to know before you open a pull
request.

## Licensing — what happens to your contribution

MeshTerm is licensed under the [Apache License, Version 2.0](LICENSE). By opening a
pull request, you agree to license your contribution under those same terms — and,
going a step beyond what Apache-2.0 alone requires, you additionally grant Jean-Pierre
Martineau (the project's copyright holder) the right to relicense your contribution as
part of the MeshTerm project, should the project's license ever change in the future.
You keep copyright over what you write; you're not signing it away, just granting these
usage rights alongside it. No separate form, bot, or signature is needed — opening the
PR is how you agree.

If that's not something you're comfortable granting, say so on the PR before it's
merged and we'll talk about it, but by default that's the deal for anything merged into
this repository.

## Before you start

- Read [CLAUDE.md](CLAUDE.md). It isn't just instructions for AI assistants — it's the
  actual style guide for this codebase: the terminology (node vs. contact, heard vs.
  seen, etc.), UX standards for screens/dialogs/menus, and where the reusable building
  blocks live in code (`ui/menus.py`, `ui/markdown.py`, `ui/widgets.py`, `ui/theme.py`).
  New code should read like it always belonged here.
- Tell us before you start working on something (see below). For anything bigger than a
  small fix, expect to talk it through first. That saves everyone a rewrite.

## Who can contribute

If you use MeshTerm, you're welcome here. Maybe something broke for you, or you wished
it could do one more thing, or a screen looked wrong on your radio. Those are exactly
the changes we want.

### How MeshTerm was made

Some people in the MeshCore community don't trust code written by AI. After some of
what has happened lately, that's easy to understand, and it deserves a straight answer.

Jean-Pierre Martineau, who started MeshTerm, sees it differently. AI was a big help on
this project. Honestly, MeshTerm couldn't have been built without it. But the choices
were his. He decided what MeshTerm should be, what it should do, and how it should
look. He uses it every day. It's handmade, with AI's help.

The project wants to stay that way. That's what the rules below are for.

### Come say hello first

Using AI tools to help you work is fine. What matters is that a real person who cares
about MeshTerm is behind the change.

The MeshTerm community lives on [Discord](https://discord.gg/AZwe5Uvb3S), and everyone
is welcome to join. But please join before you change things. Before you start writing
code, say hello in the **#contributing** channel and tell us what you'd like to work on.
Then wait until we say yes. This goes for small fixes too.

When you open your pull request, include two things:

- **Your Discord username**, the one you use on the MeshTerm server.
- **A link to the message where we said yes.** In Discord, right-click the message and
  choose "Copy Message Link."

If you haven't joined the Discord, or nobody said yes to your idea, your pull request
will be closed without a review.

### No bots

Some accounts use AI to find open issues, write a fix, and open a pull request, all on
their own. There's no person behind it who read the change or can answer questions
about it. People call this "AI slop." We close those pull requests without a review,
even if the code looks fine. The same goes for issues and bug reports written that way.

### Tell us if you used AI

This part is required. If an AI tool wrote any part of your change, say so in two
places:

- **In each commit it helped with.** Add a line at the end of the commit message naming
  the tool, like `Co-Authored-By: Claude <noreply@anthropic.com>` or
  `Assisted-by: GitHub Copilot`.
- **In your pull request.** Write a sentence about what the tool did and what you
  checked yourself.

If you used AI and didn't say so, we'll treat your pull request as a bot's. Saying so
costs you nothing. Either way, you're the author, so be ready to explain your change.

## Making a change

1. Fork the repo and branch off `main`.
2. Set up a dev install:
   ```bash
   pip install -e ".[dev]"
   git config blame.ignoreRevsFile .git-blame-ignore-revs   # skip the formatting sweeps
   ```
3. Before opening the PR, make sure these all pass:
   ```bash
   python -m pytest -q       # tests, including tests/test_gallery.py — the dual-platform
                              # readability gate (screens must stay readable at 72 cols
                              # regular / 53 cols PicoCalc; every PicoCalc case is a hard gate)
   ruff check .               # lint
   ruff format --check .      # formatting
   ```
4. Keep commits focused; write imperative-mood messages and explain *why* when it isn't
   obvious from the diff.
5. Open the PR against `main` with a clear description of what changed and why.

## Code style

- Match the surrounding file: naming, comment density, idiom.
- Follow the lexicon and UX standards in [CLAUDE.md](CLAUDE.md) — one term per concept;
  don't introduce a synonym for something that already has a name.
- New screens, dialogs, and rows go through the existing helpers rather than hand-rolled
  layout — CLAUDE.md names the enforcement points.

## Reporting bugs / requesting features

Open a GitHub issue. For bugs, include what you expected, what happened, and enough to
reproduce it — device/platform, MeshCore firmware version if relevant, and steps.

The [bug form](https://github.com/jpmartineau/MeshTerm/issues/new?template=bug.yml) asks
for all of this. Attach the file that `meshterm diagnostics --out bug.md` writes. No
GitHub account? Post it in the #bugs forum on [Discord](https://discord.gg/AZwe5Uvb3S)
instead.

---

Questions about any of this, licensing included, are welcome as a GitHub issue before you
put work in.
