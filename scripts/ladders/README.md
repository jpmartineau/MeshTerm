# Ladders

A ladder is a small script that draws the candidates for a visual value side by side, in
the user's own terminal, with MeshTerm's own renderer. JP looks at the ladder and chooses
the value. Then the value goes into the code, with the date and a reference to the ladder.

## Why we use ladders

Some values of the visual language are a decision of the eye. Two examples: how far apart
two chip fills must be before the seam between them is solid, and how light the hue of a
node is. A calculation cannot choose such a value. The measures (the OKLab distance, the
APCA contrast) only put limits on it. Also, two terminals with different fonts and colour
settings show the same value differently. Thus the eye of the user, on the user's own
terminal, is the authority.

A ladder makes this decision fast. One look at all the candidates replaces many rounds of
"change it to X and run the app again".

## The method

1. **Measure the problem first.** Find a number that shows the problem, for example the
   contrast of each chip (`contrast.py`). The number tells you which values to put on the
   ladder. Later, it goes into the comment of the constant and into the commit message.
2. **Draw with the real renderer.** A ladder imports `meshterm` from this checkout and
   draws through the same functions as the app (`PathLine`, `node_style`, `_chip_fill`).
   It never copies a colour or a rule. Thus the ladder shows what the app will show.
3. **Read the live constants.** Each ladder prints the value that the code has now, and
   draws it as one of the rows. An argument moves the value, so that the user can compare
   values without an edit.
4. **Draw each candidate in each form that the app draws.** A hue is a chip, a name in a
   route, and a name on the page. A seam is solid or thin. Put the difficult cases on the
   ladder: the weakest and the strongest hues, and the nearest neighbours.
5. **Put one line of numbers on each candidate.** The numbers do not decide. But they
   show what the eye sees, and they let you write the reason in the code.
6. **Let the user choose, in rounds.** The first ladder is wide (for example, a lightness
   from 0.70 to 0.85). The next ones are narrower, around the value that the user names.
7. **Write the decision where it is used.** The constant gets the value, the date, "JP on
   a ladder", and the path of the ladder. Add the decision to the table below.

## The ladders

| Script | What it draws | Constant | Decisions |
|---|---|---|---|
| `hue_ladder.py` | The node wheel: the HSV wheel of before, the live spectrum, and candidate wheels at one OKLab lightness each, as chips and as names | `theme._NODE_LIGHTNESS` | 0.72 (JP, 2026-10-29) |
| `seam_ladder.py` | Pairs of chips sorted by the OKLab distance of their fills, each with a solid and a thin seam, and a line at the threshold | `pathline.SEAM_BLUR` | 0.06 (JP, 2026-09-16) |
| `contrast.py` | Not a ladder: a table of the contrast between the label and the fill of each node chip (OKLab, WCAG 2, APCA) | | |

`ladderkit.py` holds the parts that all the ladders share: the console that keeps its
colour, and the two contrast measures.

## Run a ladder

In your own terminal, from the root of the repository:

```
python scripts/ladders/hue_ladder.py --L 0.70 0.75
python scripts/ladders/seam_ladder.py --blur 0.08
python scripts/ladders/contrast.py --hsv
```

Each script has `--help`. The terminal must have truecolor and a font with the powerline
glyphs, as for the chips of the app.

## When Claude opens the window

On Windows, Claude Code can open a ladder in a new Windows Terminal window for JP:

```
scripts\ladders\launch.ps1 hue_ladder --L 0.70 0.75
scripts\ladders\launch.ps1 -Snap seam_ladder
```

`launch.ps1` opens the window and waits until the ladder writes its marker file. Then it
prints what the ladder found: the colour system, virtual terminal processing, and if
stdout is the console. With `-Snap`, it also takes a screenshot of the window
(`snap.ps1`). Claude examines that picture before it tells JP that the ladder is ready. In
the new window, `ladder <arguments>` runs the same ladder again with other values. The
path of the checkout must have no spaces (refer to trap 1).

Three traps made the first launches fail. Two sessions met all three on the same night
(2026-10-28 and 2026-10-29), and the second session did not know that the first had solved
them. This folder keeps the solution in one place. The launcher stops each trap:

1. **Windows Terminal gives the command line as one name.** When `wt.exe` sends a new tab
   to a window that is already open, it puts quotes around the whole command line. Then
   Windows looks for one file whose name is the whole line, and the tab shows the error
   0x80070002 ("The system cannot find the file specified"). Thus the tab runs `run.cmd`,
   whose path has no spaces, with no arguments. The name of the ladder and its arguments
   go in environment variables (`LADDER_NAME`, `LADDER_ARGS`), because the tab gets the
   environment of the process that runs `wt.exe`.
2. **The new tab gets the environment of Claude Code.** Claude Code runs its commands with
   `NO_COLOR=1`, and Rich removes all colour when it sees that variable. The tab got it
   too, so the first ladder was black and white. `run.cmd` and `run.ps1` remove it, and
   the console of the ladder sets `no_color=False`.
3. **Rich uses the console API of Windows.** On Windows, Rich examines the console mode
   of stdout. When virtual terminal processing is off, Rich does not write ANSI codes. It
   uses the console API of Windows, which has 16 colours, and which writes no colour at
   all into a capture. `ladderkit.console` turns the processing on and sets
   `legacy_windows=False`. Also, `run.ps1` starts Python with `Start-Process
   -NoNewWindow`, so that Python writes to the console itself and not to a PowerShell pipe.

The most important lesson is not a trap: **do not tell the user that a window is ready
before you check it.** A launch that fails prints nothing in the shell that started it, and
a ladder with no colour runs with no error. Only the marker file and the screenshot show
the result. On 2026-10-29, Claude told JP two times that the ladder was ready, and each
time it was not.

## Keep them working

The ladders use private names of the package (`_chip_fill`, `_spectrum_colours`,
`_NODE_HUE_SAT`). When the code changes, a ladder can break, and the tests do not run the
ladders. Correct a ladder when you use it again, and keep it in this folder. A new ladder
also goes here, with a row in the table above.
