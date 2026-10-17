---
name: website-media
description: Make the website's demo video and screenshots again from the demo mesh — the terminal video on meshterm.net and in MeshTerm's README (scripts/video.py) and the twelve screenshots of the README and the website (scripts/screenshots.py) — check them, upload the video for the README, and commit them in both repositories. Use when JP asks to make, redo, refresh, or update the video, the demo recording, the screenshots, the website's images, or the website after a visible change; the release skill also runs it after each version bump.
---

# Making the website's video and screenshots

The two always go together (JP, 2026-10-17: "include it with the screenshot updates when we
update the website, manually or automatically when we release"). Both come from the demo
mesh (`meshterm --demo`), headless, and both show the version in their header, so a set
made before a bump is already stale.

| What | Script | Where it goes | Time |
|---|---|---|---|
| 12 screenshots + 2 splash wordmarks | `scripts/screenshots.py` | MeshTerm `docs/screenshots/` | ~1 min |
| The demo video, its poster, its play card | `scripts/video.py` | meshterm.net `src/demo.mp4`, `src/demo-poster.png`, `src/demo-play.png` | ~4 min |
| The README's copy of the video | `scripts/video.py --readme` | a GitHub attachment, and its URL in MeshTerm `README.md` | ~10 s |

**The video is terminal-only, on purpose** (JP, 2026-10-17: "it's marketing after all").
Don't offer handheld videos again.

**Why the video lives in the site repository and not in the tag:** it is ~4 MB, and it
changes at every release (the version is on screen). In MeshTerm, that would grow every
clone of the code by 4 MB a release. The site repository already keeps the copy it serves.
The cost is one more push at release time (see *Release mode*).

## Before you start

- The site checkout must sit beside this one, at `../meshterm.net`. The video script
  writes there by default and stops if the folder is missing.
- The site must be in sync and clean where the video goes:

  ```
  git -C ../meshterm.net fetch -q
  git -C ../meshterm.net status -sb
  ```

  If it is behind (the release workflow's `website` job pushes a "MeshTerm X.Y.Z" commit
  each release), `git -C ../meshterm.net pull --ff-only`. If `src/demo*` has uncommitted
  changes you didn't make, stop and ask.
- Needs: the `dev` extras (Pillow, fontTools, time-machine), the emulator's Terminus font
  (`meshterm emulate --fetch-fonts`, once), Windows' Cascadia Mono and Segoe UI fonts, the
  network for the map's tiles the first time, and ffmpeg with libx264 on the `PATH`.

## 1. Run both, one after the other, in the background

```
.venv/Scripts/python scripts/screenshots.py
.venv/Scripts/python scripts/video.py
```

One background command for both, in this order. **Never in parallel**, and don't run the
test suite beside them: the video's tour runs on the real clock and its painter uses every
core. Both are driven by waits on screen text, so a slow machine only makes them slower.

The video prints its length and size at the end (`recorded 118.9 s, 189 screens`, then
`demo.mp4 (4001 KB)`).

## 2. Look at what you made

A green run is not a correct picture. Check both:

- **Screenshots:** `git status --short docs/screenshots`, then open one image of each set.
  Only the version in the header should change, plus whatever this change set out to
  change on those screens. Anything else is a bug to understand, not a picture to ship.
- **Video length:** under **2:00**. The site's caption says "Under two minutes". If it
  went over, shorten holds in `_tour` (`p.hold(...)`), never the scenes JP asked for.
- **Video content:** make a contact sheet and look at it:

  ```
  ffmpeg -v error -y -i ../meshterm.net/src/demo.mp4 -vf "fps=1/4,scale=400:-1,tile=6x5" -frames:v 1 <scratchpad>/sheet.png
  ```

  The tour, in order (JP chose each part; keep them all):
  1. the main menu;
  2. Chat → Public → the paths of Gabriel's message from Beloeil, selected 1, 2, 1, 2;
  3. Chloé's message arrives live; we type "Hey, I wonder if @[Dave-Lachine] is coming
     Saturday?" — the mention turns into a chip when the `]` is typed — and Dave answers
     with a mention of us, so his message carries our white ★;
  4. Trace target → Brossard, 2 bytes per hop (our node uses 3; a trace collapses it to
     2), the "5 new records" dialog, then the per-hop table;
  5. the map: pan, ^L to our node, zoom 10 → 16 until street names and buildings show;
  6. Repeater admin on Mont-Royal: Read settings (the video jumps over most of it), then
     Send advert;
  7. the live feed, one packet opened, and back to a clean main menu.

  Also open `src/demo-poster.png`: it must show the paths dialog with the **second** path
  selected.

## 2b. Upload it for the README — only once the video is right

MeshTerm's README plays its own copy of the video: GitHub plays a video in a README only
when the file is one of its own attachments. Once the video passed step 2:

```
.venv/Scripts/python scripts/video.py --readme
git diff README.md
```

It uploads `../meshterm.net/src/demo.mp4` through GitHub's undocumented attachment endpoint
(`uploads.github.com/user-attachments/assets`, with the `gh` token: no issue, no browser),
and replaces the one `https://github.com/user-attachments/assets/…` line of `README.md`.
The diff must be that one line. Each upload is public and permanent (GitHub cannot delete
an attachment, and the old ones stay, unlinked), so **never run it on a video you haven't
checked**, and never in a loop.

If GitHub refuses it (the endpoint is undocumented: it can change or go away), the README
keeps the previous video, which is wrong but not broken. Tell JP. The fallback is the web
page: he drops `src/demo.mp4` into a comment box on the repository and gives you the URL to
put on that line.

## 3. When it fails

`video.py` prints `the recording failed (1)` after a traceback. Read the traceback's last
line: a `TimeoutError` carries the whole screen as it was.

- **A wait never matched** (`'…' did not appear`): a UX string the tour waits for changed,
  or a dialog the tour doesn't expect is in the way. The video's own history: the trace
  once got a records dialog when the path width changed, and a menu filter "live" matched
  Courier first. Fix the tour in `scripts/video.py`, not the app — unless the screen shows
  an app bug, which then gets fixed and reported to JP.
- **The trace says "no reply":** the demo's route to that target includes a link the
  world doesn't have. Brossard is the target because its route always works; see the
  `_tour` comment before changing it.
- **To iterate on painting or encoding** without recording again, pass
  `--recording <scratchpad>/rec.pickle`: the first run keeps the recording there, a later
  run with the same path only paints and encodes. Never keep a recording across a change
  to the app.

If you cannot make it work, stop and ask JP whether to keep last time's video. Never ship
half a set.

## 4. Commit

MeshTerm, by name (other sessions share the tree):

```
git add docs/screenshots README.md
```

The site, the three source files only:

```
git -C ../meshterm.net add src/demo.mp4 src/demo-poster.png src/demo-play.png
git -C ../meshterm.net commit -m "The demo video of MeshTerm X.Y.Z" -m "Co-Authored-By: <the model running this> <noreply@anthropic.com>"
```

Then one of the two modes.

### Manual mode — JP asked for the website now

Commit the screenshots and the README's new video URL in MeshTerm ("The screenshots
show …", what changed).
Then rebuild the site so its `docs/` serves the new files:

```
cd ../meshterm.net && ../MeshTerm/.venv/Scripts/python build.py
```

`build.py` writes seven derived images again with new bytes even when nothing changed.
Unless the splash art changed (`docs/screenshots/splash/` in MeshTerm), restore them:

```
git -C ../meshterm.net checkout -- docs/assets/apple-touch-icon.png docs/assets/logo-96.png docs/assets/og.png docs/assets/social-preview.png docs/assets/splash-narrow.png docs/assets/splash.png docs/favicon.ico
```

Commit the built `docs/` with the source files (one commit). **Don't push**: tell JP the
site is committed and ahead of origin. The live site changes only when he pushes it. Note
that the screenshots the site shows come from the MeshTerm checkout, so they are the
working tree's, not a release's.

### Release mode — called from the release skill, after the bump

Don't build the site locally: the release workflow's `website` job builds it from the tag
and from the site's `main`. Commit the site's three source files as above, and the
screenshots and the README ride in the release commit (the release skill stages
`docs/screenshots` and `README.md`).

**The site's commit must be pushed before the tag**, or the job builds with last
release's video. The release skill's push step does it, in the same confirmed push. After
the job has run, `git -C ../meshterm.net pull --ff-only` picks up its "MeshTerm X.Y.Z"
commit.
