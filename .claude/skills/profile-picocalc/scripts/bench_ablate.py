"""Decompose a repaint by ablation — the honest alternative to cProfile on this CPU.

cProfile's per-call overhead badly distorts a path made of tens of thousands of tiny
calls, so instead of trusting its attribution we remove one stage at a time from a real
render loop and read the difference.

Stages, for one screen:
  compose    MeshTerm's frame.compose_base -> the ANSI frame string
  parse      prompt_toolkit's ANSI(text) -> fragments
  pt-1row    pt's Window write + screen diff + terminal output, one row differing
  pt-full    the same, every row differing (a scroll or a screen change)
  pt-same    the same, frame byte-identical (the idle tick)

Usage: python bench_ablate.py [--only NAME,...] [--reps 20]
"""

from __future__ import annotations

import argparse
import time

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.text import Text

from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.ui.tui.session import TuiSession


class _Sink:
    def __init__(self):
        self.n = 0
        self.last = 0

    def write(self, data):
        self.n += len(data)
        return len(data)

    def flush(self):
        pass

    def isatty(self):
        return True

    encoding = "utf-8"


def build(name, cols, rows):
    from tests import test_gallery as g

    for e in g._ENTRIES:
        if e.name == name:
            return e.factory(cols, rows)
    raise SystemExit(f"unknown screen {name!r}")


def timeit(fn, reps):
    fn()
    t = time.perf_counter()
    for _ in range(reps):
        fn()
    return (time.perf_counter() - t) / reps * 1000


def measure(name, cols, rows, reps):
    sink = _Sink()
    out = Vt100_Output(sink, lambda: Size(rows=rows, columns=cols), term="linux")
    with create_pipe_input() as inp:
        session = TuiSession(
            header=lambda c: Text("MeshTerm v0.9  ·  mesh 12 nodes  ·  ▂▃▅▂▁", style="brand"),
            input=inp,
            output=out,
        )
        session.push(build(name, cols, rows))
        app = session._build_app()
        session._app = app
        app._is_running = True
        with set_app(app):
            renderer = app.renderer
            for _ in range(3):
                renderer.render(app, app.layout)

            # --- compose: MeshTerm's own frame build, cursor moving each time ---
            def compose_once():
                session._dispatch("down")
                return session._render_base()

            compose_ms = timeit(compose_once, reps)

            # Two real, adjacent frames (a cursor step apart) and a far-apart pair.
            session._dispatch("down")
            f_a = session._render_base().value
            session._dispatch("down")
            f_b = session._render_base().value
            for _ in range(12):
                session._dispatch("down")
            f_far = session._render_base().value

            # --- parse: pt's ANSI() over a whole frame ---
            parse_ms = timeit(lambda: ANSI(f_a), reps)

            # --- pt render floor: pre-parsed content, so only Window+diff+output run ---
            pre_a, pre_b, pre_far = ANSI(f_a), ANSI(f_b), ANSI(f_far)
            box = {"cur": pre_a}
            base_window = app.layout.container.content
            base_window.content.text = lambda: box["cur"]
            # FormattedTextControl caches on the *result* of the callable; distinct ANSI
            # objects hash by identity, so alternating them defeats it exactly as a real
            # content change would.
            renderer.render(app, app.layout)

            def flip(pair):
                i = {"n": 0}

                def go():
                    i["n"] ^= 1
                    box["cur"] = pair[i["n"]]
                    renderer.render(app, app.layout)

                return go

            pt_1row = timeit(flip((pre_a, pre_b)), reps)
            pt_full = timeit(flip((pre_a, pre_far)), reps)

            box["cur"] = pre_a
            renderer.render(app, app.layout)
            pt_same = timeit(lambda: renderer.render(app, app.layout), reps)

            # How different are the two frames, in rows?
            rows_a, rows_b = f_a.split("\n"), f_b.split("\n")
            changed = sum(1 for x, y in zip(rows_a, rows_b, strict=True) if x != y)
            rows_far = f_far.split("\n")
            changed_far = sum(1 for x, y in zip(rows_a, rows_far, strict=True) if x != y)
    return {
        "compose": compose_ms,
        "parse": parse_ms,
        "pt_1row": pt_1row,
        "pt_full": pt_full,
        "pt_same": pt_same,
        "changed": changed,
        "changed_far": changed_far,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--platform", default="picocalc-lyra")
    args = ap.parse_args()

    set_platform(PICOCALC_LYRA if args.platform == "picocalc-lyra" else REGULAR)
    cols, rows = (53, 26) if args.platform == "picocalc-lyra" else (72, 24)

    from tests import test_gallery as g

    names = [e.name for e in g._ENTRIES]
    if args.only:
        want = set(args.only.split(","))
        names = [n for n in names if n in want]

    print(f"# repaint ablation — {args.platform} {cols}x{rows}, {args.reps} reps")
    print(f"{'screen':<16} {'compose':>8} {'parse':>7} | {'pt-1row':>8} {'pt-full':>8} "
          f"{'pt-same':>8} | {'rows∆':>6} {'far∆':>5} | {'total*':>7}")
    print("-" * 92)
    for name in names:
        try:
            m = measure(name, cols, rows, args.reps)
        except Exception as exc:  # noqa: BLE001
            print(f"{name:<16} FAILED: {type(exc).__name__}: {exc}")
            continue
        total = m["compose"] + m["parse"] + m["pt_1row"]
        print(f"{name:<16} {m['compose']:7.1f} {m['parse']:6.1f} | "
              f"{m['pt_1row']:7.1f} {m['pt_full']:7.1f} {m['pt_same']:7.1f} | "
              f"{m['changed']:6d} {m['changed_far']:5d} | {total:6.1f}")
    print("\n* total = compose + parse + pt-1row (the per-keystroke path, "
          "excluding input+scheduling)")


if __name__ == "__main__":
    main()
