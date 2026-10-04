---
name: release
description: Cut a MeshTerm release — pick the version, write the changelog entry from the commits since the last tag, bump `__version__`, commit, tag, push so the GitHub release builds, and hand JP a Discord announcement to post. Use when JP says to release, cut a release, ship a version, tag a version, or bump the version, and when asked what would go into the next release.
---

# Cutting a MeshTerm release

A release is a **rename and a tag**, not a week of git archaeology — that is the whole
point of keeping an `[Unreleased]` section. The one part that takes real work is writing
the entry, because the changelog is prose someone reads, not a list of commit subjects.

Pushing the tag fires [github-release.yml](../../../.github/workflows/github-release.yml),
which calls `installers.yml`, builds all five platforms, attaches them with a combined
`SHA256SUMS`, and takes the release notes **from that version's changelog section**. So the
changelog is not documentation of the release; it *is* the release notes. An empty section
publishes "No changelog entry for X.Y.Z."

## Two things this skill never does

- **Never runs `release.yml`.** That is the **PyPI** publish, and it is deliberately hard
  to fire: no automatic trigger, a typed version that must match the build, and a `pypi`
  environment gate. PyPI has no rename and no true delete. It runs only when JP says so in
  that moment, from the Actions tab, by hand. The name `meshterm` is on PyPI's prohibited
  list anyway — the project publishes as `mesh-term`.
- **Never picks the version silently when it isn't a patch.** Default is a **patch** bump
  (`0.3.0` → `0.3.1`). Anything else — minor, major — is JP's call, and he says which part.
  Bumping one part zeroes the ones after it. While the major is `0`, a **minor** bump is
  allowed to change behaviour, not just add to it.

## 1. Preflight

```
python .claude/skills/release/release_check.py
```

It prints the current version, the last tag, every commit since it, the state of the
`[Unreleased]` section, any changelog link refs that are missing, and whether the tree is
clean and in sync with `origin/main`. Read it before touching anything — it is also the
answer to "what would go into the next release?" on its own.

Then the gates `CONTRIBUTING.md` asks contributors to pass:

```
python -m pytest -q && ruff check . && ruff format --check .
```

A release does not go out on a red suite. If something fails, stop and say so.

## 2. Decide the version

**Always ask, as a multiple choice**, whenever JP asks for a release — even when his
request names a bump, because words like "minor" have meant "a small one" before (he
asked to "bump minor" and meant the patch). One AskUserQuestion, three choices, each
showing the actual number it produces:

- **Patch — X.Y.(Z+1)** (Recommended)
- **Minor — X.(Y+1).0**
- **Major — (X+1).0.0**

**Major asks twice.** If he picks it, ask a second, separate question before writing the
number anywhere — "Release (X+1).0.0? A major version is a promise about compatibility
that can't be taken back." — with *Yes, (X+1).0.0* and *No, go back to patch* as its
choices. Only a second yes makes it major.

Then say the number you arrived at before you write it anywhere, so a wrong assumption
costs a sentence instead of a tag.

**Don't recommend a bigger bump**, however many features or changed defaults the release
carries — not even on the strength of the changelog's SemVer note. 0.10.0 went out that way
on a recommendation, and JP would have preferred 0.9.1. A minor or major bump happens only
when he names it.

## 3. Write the entry

This is the work. Read the commits since the last tag (`git log --stat v<last>..HEAD`) and
write **what changed for someone using MeshTerm**, in the changelog's existing voice:

- **Categories are Keep a Changelog's** — `### Added`, `### Changed`, `### Fixed`,
  `### Removed` — in that order, and only the ones that have entries.
- **A bullet opens with a bolded sentence stating the user-visible fact**, then a paragraph
  saying what was actually wrong and what it now does. Read the `0.2.6` and `0.2.8` entries
  for the register: the failure is described from the reader's side first, the mechanism
  second, and the lesson last if there is one.
- **Small entries are one line and no bold.** Not every commit earns a paragraph, and
  several commits often collapse into one bullet — group by what the reader experienced,
  not by what the diff touched.
- **A commit that changes nothing a user can see does not appear.** Refactors that move
  code behind an unchanged surface, test-only commits, skill and tooling commits: leave
  them out. The changelog is not a shadow git log.
- Wrap at the file's width (~95 columns) and match its em-dash-and-clause rhythm.

Insert the section directly under the HTML comment at the top of `CHANGELOG.md`:

```markdown
## [Unreleased]

## [X.Y.Z] — YYYY-MM-DD
```

A fresh empty `[Unreleased]` heading goes back in above it — that is what makes the *next*
release a rename. Then add the link refs at the bottom, repointing `[Unreleased]` at the
new tag:

```
[Unreleased]: https://github.com/jpmartineau/MeshTerm/compare/vX.Y.Z...HEAD
[X.Y.Z]: https://github.com/jpmartineau/MeshTerm/releases/tag/vX.Y.Z
```

The date is the real date — check it rather than copying the one above.

## 4. Bump the version

`__version__` in [meshterm/__init__.py](../../../meshterm/__init__.py) is **the only place a
version is written**. `[project] version` in `pyproject.toml` is `dynamic` and hatchling
reads the attribute, so the number in a built wheel cannot drift from the number in the
source. Do not add a second one.

### The README needs nothing — deliberately

It used to. `installers.yml` names each asset `meshterm-${version}-<label><ext>`, so every
download command in the README named a version, and every release meant grepping the old
number out of five of them. That step failed quietly when it was missed: a README naming
last release's file still reads perfectly well, and only breaks for the one person who
types it.

`github-release.yml` now attaches a **version-less copy of every binary** beside the
stamped one, so `releases/latest/download/meshterm-macos-arm64` is a URL that never moves —
which is what the README and `docs/devices/uconsole.md` use. Nothing in either names a version.
Leave them alone, and if you find yourself adding a version to an install command, add an
alias to the gather step instead.

What *does* still carry the number: `docs/cli/README.md` has a sample document with a `"version"`
field, which is the live `{version}` placeholder and must show the new one.

## 5. Commit, tag, push

```
git commit -a -m "MeshTerm X.Y.Z" -m "Co-Authored-By: <the model running this skill> <noreply@anthropic.com>"
git tag -a vX.Y.Z -m "MeshTerm X.Y.Z"
```

The trailer is not optional: the changelog entry in this commit is prose you wrote, and
CONTRIBUTING.md asks every contributor to say when an AI tool wrote part of a change —
the project's own commits say it first. Name the model actually running, not a
hardcoded one. Seven release commits (0.3.1 through 0.10.0) went out without it because
this line used to omit it.

Commit on `main` — never a topic branch. Then **confirm with JP before pushing**, because
the push is the act that builds and publishes the release:

```
git push origin main && git push origin vX.Y.Z
```

This is the one workflow where pushing is expected. It is still asked for, once, in that
moment — the standing rule against unasked pushes is not suspended by the skill, it is
satisfied by the answer.

## 6. Watch it land

```
gh run watch --exit-status
gh release view vX.Y.Z
```

Five platform builds, the release job, and `website` — which rebuilds meshterm.net from the
tag so the site shows the new version — so it takes a few minutes. If only `website` fails,
the release itself is fine (it runs after publishing); the usual cause is the
`SITE_DEPLOY_KEY` secret, and once that is fixed `gh run rerun <run-id> --failed` finishes
it.

The notes open with an install block the workflow writes from the tag — the same commands
as the README, but pinned to *this* release's own assets — and the changelog follows under
`## What changed`. **Check that second half actually came out of the changelog**, because a
green run is not proof: the fallback is a successful step.

```
gh release view vX.Y.Z --json body --jq .body | sed -n '/## What changed/,$p' | head -5
```

If that reads "No changelog entry for X.Y.Z.", the extraction failed rather than the
changelog being empty. Fix the workflow, then repair the published notes in place with
`gh release edit vX.Y.Z --notes-file` — the binaries are fine and the tag does not move.

Fourteen assets is the right count — five version-stamped binaries, five version-less
copies of them, `SHA256SUMS`, and the three licence files:

```
gh release view vX.Y.Z --json assets --jq '.assets | length'
```

If a *build* fails, the tag is already public: fix forward with a new patch version rather
than deleting and re-pushing a tag people may have fetched.

## 7. The Discord announcement — for JP to post

Once the release has landed and checked out, end by showing JP a short announcement for
the Discord server's `#announcements` channel, in a code block he can copy as-is. **He
posts it himself** — never post it, never hand it to a bot. Write it for the people on
the server, in plain words (they use MeshTerm; they don't read the diff):

- one bolded opening line naming the version and what it is, in a few words;
- two to five short bullets of what changed *for them*, from the changelog entry —
  the headline items only, not every line;
- the release link, and one line on how to get it (the download page, or the README's
  install line for someone who installed that way);
- Discord markdown only (`**bold**`, `- ` bullets, bare links); no headings, no tables,
  no emoji beyond one at the start if it fits. Well under Discord's 2000 characters —
  it should fit on a phone screen.

```
**MeshTerm 0.10.1 is out**: three small command-line fixes.

- `records --width` now tells you when a width doesn't exist, instead of looking empty
- The simulator's row in `devices` lost a stray `()`
- `platform --help` and `specimen --help` read like the rest

Download: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.10.1
```

Say who it was written for, then show it last in the reply, so it's the easiest thing to
copy.

## At 0.9.0 the changelog resets

The first public release collapses everything below it into a single entry reading "first
public release" with the headline features under it. Every version under 0.9.0 was released
nowhere, and its entries are notes to ourselves about getting ready — nobody arriving on
launch day wants a changelog of the fortnight before it. **Keep the dates and the tags;
replace the prose.** The same note is an HTML comment at the top of `CHANGELOG.md`, where
whoever cuts it will be looking.
