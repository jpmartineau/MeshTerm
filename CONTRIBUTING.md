# Contributing to MeshTerm

Thank you for your wish to work on MeshTerm. Read these notes before you open a pull
request.

## Licensing: what happens to your contribution

MeshTerm is licensed under the [Apache License, Version 2.0](LICENSE). When you open a
pull request, you agree to license your contribution under the same terms. You also agree
to one more term that Apache-2.0 alone does not require. You grant Jean-Pierre Martineau
(the project's copyright holder) the right to relicense your contribution as part of
the MeshTerm project, if the project's license changes in the future.
You keep the copyright of what you write. You do not give it away. You only grant these
rights of use in addition to it. You do not need a separate form, bot, or signature. When
you open the pull request, you agree.

If you are not comfortable with this grant, say so on the pull request before we merge it,
and we will talk about it. If you do not say so, these are the terms for each contribution
that we merge into this repository.

## Before you start

- Read [CLAUDE.md](CLAUDE.md). It is not only instructions for AI assistants. It is the
  real style guide for this codebase. It gives the terms (node and contact, heard and
  seen, and other terms), the UX standards for screens, dialogs, and menus, and the place
  in the code of each building block that you can use again (`ui/menus.py`,
  `ui/markdown.py`, `ui/widgets.py`, `ui/theme.py`). New code must look like it was always
  part of the project.
- Tell us before you start work on a change (refer to the sections below). If a change is
  bigger than a small correction, expect to talk about it first. This saves everyone from
  a rewrite.

## Who can contribute

If you use MeshTerm, you are welcome here. Maybe something did not work for you. Maybe you
wanted one more function. Maybe a screen looked wrong on your radio. We want exactly these
changes.

### How MeshTerm was made

Some persons in the MeshCore community do not trust code that AI wrote. After some recent
events, we understand this, and it needs a direct answer.

Jean-Pierre Martineau started MeshTerm. He has a different opinion. AI helped much with
this project. Honestly, it was not possible to build MeshTerm without it. But he made the
choices. He decided what MeshTerm must be, what it must do, and how it must look. He uses
it each day. It is handmade, with the help of AI.

The project must stay this way. The rules below are for this purpose.

### Come say hello first

It is permitted to use AI tools to help you work. What is important is that a real person
who cares about MeshTerm is behind the change.

The MeshTerm community is on [Discord](https://discord.gg/AZwe5Uvb3S), and everyone is
welcome to join. But join before you change anything. Before you start to write code,
say hello in the **#contributing** channel and tell us what you want to work on. Then wait
until we say yes. This is also the rule for small corrections.

When you open your pull request, include these two items:

- **Your Discord username.** Use the one that you use on the MeshTerm server.
- **A link to the message where we said yes.** In Discord, right-click the message and
  choose "Copy Message Link."

If you did not join the Discord, or if nobody said yes to your idea, we will close your
pull request without a review.

### No bots

Some accounts use AI to find open issues, write a correction, and open a pull request, all
by themselves. No person read the change, and no person can answer questions about it.
People call this "AI slop." We close these pull requests without a review, also when the
code looks correct. The same rule is true for issues and bug reports that are written in
this way.

### Tell us if you used AI

This part is necessary. If an AI tool wrote any part of your change, you must say so in two
places:

- **In each commit that it helped with.** Add a line at the end of the commit message with
  the name of the tool, for example `Co-Authored-By: Claude <noreply@anthropic.com>` or
  `Assisted-by: GitHub Copilot`.
- **In your pull request.** Write a sentence about what the tool did and what you checked
  yourself.

If you used AI and you did not say so, we will treat your pull request as a bot's pull
request. It costs you nothing to say so. In all cases, you are the author, so be ready to
explain your change.

## Making a change

1. Fork the repository and make a branch from `main`.
2. Set up a dev install:
   ```bash
   pip install -e ".[dev]"
   git config blame.ignoreRevsFile .git-blame-ignore-revs   # skip the formatting sweeps
   ```
3. Before you open the pull request, make sure that all these checks pass:
   ```bash
   python -m pytest -q       # tests, including tests/test_gallery.py — the dual-platform
                              # readability gate (screens must stay readable at 72 cols
                              # regular / 53 cols PicoCalc; every PicoCalc case is a hard gate)
   ruff check .               # lint
   ruff format --check .      # formatting
   ```
   The tests include `tests/test_writing_style.py`. It checks the docstrings and the
   comments for the marks that ASD-STE100 does not use. Refer to
   [How we write](docs/development/writing-style.md).
4. Keep each commit to one subject. Write the commit message in the imperative mood. Explain
   *why* when the diff does not show the reason.
5. Open the pull request against `main`. Write a clear description of what changed and why.

## Code style

- Make your change match the file around it: the names, the amount of comments, and the
  idioms.
- Follow the terms and the UX standards in [CLAUDE.md](CLAUDE.md). Use one term for each
  concept. Do not add a synonym for something that already has a name.
- Build new screens, dialogs, and rows with the existing helpers. Do not make the layout by
  hand. CLAUDE.md names the places where the code enforces these standards.
- Write docstrings, comments, and documents in ASD-STE100 Simplified Technical English,
  with Canadian spelling. [How we write](docs/development/writing-style.md) gives the
  rules and the glossary.

## Reporting bugs / requesting features

Open a GitHub issue. For a bug, include what you expected, what happened, and enough
information for us to reproduce it. This is the device and platform, the MeshCore firmware
version if it is relevant, and the steps.

The [bug form](https://github.com/jpmartineau/MeshTerm/issues/new?template=bug.yml) asks
for all of this. Attach the file that `meshterm diagnostics --out bug.md` writes. If you do
not have a GitHub account, post the report in the #bugs forum on
[Discord](https://discord.gg/AZwe5Uvb3S) instead.

---

You can ask a question about any of this, and this includes licensing, as a GitHub issue
before you start work.
