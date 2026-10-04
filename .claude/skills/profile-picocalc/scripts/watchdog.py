# SPDX-License-Identifier: Apache-2.0
"""Run the real MeshTerm with a freeze watchdog: what stopped the app, not just how long.

The per-key clock (drive_console.py) says a key took four seconds; this says why. A thread
outside the event loop pings it every 50 ms, and while a ping goes unanswered it samples
every thread's stack — so a stall shows whether the loop was busy itself or waiting its
turn for the GIL behind a worker. Alongside: the process's RSS, swap and major page
faults twice a second, which is how a *whole-process* freeze shows up (the log itself goes
silent, and majflt jumps by thousands: the kernel is paging the interpreter back in from
the SD-card swap), and timing for the map's tile loads and rasters.

Writes ~/tmp/watchdog.log (override with $WATCHDOG_LOG). Events, one per line, all with
seconds since start:

  ping   lag_ms                      event-loop latency of a call_soon_threadsafe ping
  stall  lag_ms  thread=...  stack   a stack sample of every thread while the loop is stuck
  mem    rss_mb swap_mb majflt cpu_ms threads queue pending tiles drawing wanted memo
  tile   op ms key thread            one stage of BasemapSource.load_tile
  raster ms coarse key               one render_ground pass
  key    action                      a dispatched key
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback

LOG = os.environ.get("WATCHDOG_LOG") or os.path.expanduser("~/tmp/watchdog.log")
T0 = time.monotonic()
_fh = open(LOG, "a", buffering=1)
_lock = threading.Lock()
_state = {"loop": None, "screen": None, "source": None}


def emit(kind, *parts):
    line = f"{time.monotonic() - T0:9.3f} {kind} " + " ".join(str(p) for p in parts)
    with _lock:
        _fh.write(line + "\n")


def _proc():
    try:
        with open("/proc/self/status") as fh:
            st = {k: v.strip() for k, v in (ln.split(":", 1) for ln in fh if ":" in ln)}
        rss = int(st.get("VmRSS", "0 kB").split()[0]) / 1024
        swap = int(st.get("VmSwap", "0 kB").split()[0]) / 1024
        thr = int(st.get("Threads", "0"))
        with open("/proc/self/stat") as fh:
            f = fh.read().rsplit(")", 1)[1].split()
        majflt = int(f[9])
        cpu_ms = (int(f[11]) + int(f[12])) * 1000 / os.sysconf("SC_CLK_TCK")
        return rss, swap, majflt, cpu_ms, thr
    except Exception:  # noqa: BLE001
        return 0, 0, 0, 0, 0


def _short_stack(frame, depth=7):
    out = []
    while frame is not None and len(out) < depth:
        co = frame.f_code
        fn = co.co_filename
        mod = fn.rsplit("/", 2)
        out.append(f"{mod[-1]}:{frame.f_lineno}:{co.co_name}")
        frame = frame.f_back
    return " < ".join(out)


def watchdog():
    pending = None  # (sent_at) of the outstanding ping
    last_stall_sample = 0.0
    last_mem = 0.0
    names = {}
    while True:
        time.sleep(0.05)
        now = time.monotonic()
        loop = _state["loop"]
        if loop is None:
            continue
        if pending is None:
            sent = now

            def pong(sent=sent):
                nonlocal pending
                lag = (time.monotonic() - sent) * 1000
                if lag > 60:
                    emit("ping", f"{lag:.0f}")
                pending = None

            pending = sent
            try:
                loop.call_soon_threadsafe(pong)
            except RuntimeError:
                return
        elif now - pending > 0.25 and now - last_stall_sample > 0.25:
            last_stall_sample = now
            for t in threading.enumerate():
                names[t.ident] = t.name
            frames = sys._current_frames()
            for ident, fr in frames.items():
                if ident == threading.get_ident():
                    continue
                emit("stall", f"{(now - pending) * 1000:.0f}",
                     f"thread={names.get(ident, ident)}", _short_stack(fr))
        if now - last_mem > 0.5:
            last_mem = now
            rss, swap, majflt, cpu, thr = _proc()
            scr = _state["screen"]
            src = _state["source"]
            q = "-"
            try:
                ex = loop._default_executor
                if ex is not None:
                    q = f"{ex._work_queue.qsize()}/{len(ex._threads)}"
            except Exception:  # noqa: BLE001
                pass
            if scr is not None:
                info = (f"pending={len(scr._pending)} tiles={len(scr._tiles)} "
                        f"drawing={'y' if scr._drawing else 'n'} "
                        f"wanted={'y' if scr._wanted else 'n'} spec={'y' if scr._speculating else 'n'}")
            else:
                info = "-"
            memo = (f"{len(src._memo)}/{getattr(src, '_memo_bytes', 0) / 1048576:.1f}MB" if src is not None else "-")
            emit("mem", f"rss={rss:.1f} swap={swap:.1f} majflt={majflt} cpu={cpu:.0f}",
                 f"thr={thr} q={q} {info} memo={memo}")


def install():
    from meshterm.services import basemap
    from meshterm.ui import map_screen
    from meshterm.ui.tui.session import TuiSession

    def timed(cls, name, label):
        orig = getattr(cls, name)

        def wrapper(self, *a, **k):
            t = time.perf_counter()
            try:
                return orig(self, *a, **k)
            finally:
                _state["source"] = self
                key = a[1] if name == "_decode" else "/".join(str(x) for x in a[:3])
                emit("tile", label, f"{(time.perf_counter() - t) * 1000:.0f}",
                     key, threading.current_thread().name)

        setattr(cls, name, wrapper)

    timed(basemap.BasemapSource, "load_tile", "load")
    timed(basemap.BasemapSource, "_read_decoded", "sidecar")
    timed(basemap.BasemapSource, "_decode", "decode")
    timed(basemap.BasemapSource, "_fetch_tile", "fetch")
    timed(basemap.BasemapSource, "_write_decoded", "write")

    orig_rg = map_screen.render_ground

    def render_ground(vp, tiles, markers, *a, **k):
        t = time.perf_counter()
        try:
            return orig_rg(vp, tiles, markers, *a, **k)
        finally:
            emit("raster", f"{(time.perf_counter() - t) * 1000:.0f}",
                 f"coarse={k.get('coarse', False)}", f"z{vp.zoom}", f"tiles={len(tiles)}",
                 threading.current_thread().name)

    map_screen.render_ground = render_ground

    orig_init = map_screen.MapScreen.__init__

    def init(self, *a, **k):
        orig_init(self, *a, **k)
        import asyncio

        _state["screen"] = self
        _state["loop"] = asyncio.get_running_loop()
        emit("map", "open")

    map_screen.MapScreen.__init__ = init

    orig_dispatch = TuiSession._dispatch

    def dispatch(self, action, data=""):
        if _state["loop"] is None:  # the first key: ping the loop from here on, map or not
            import asyncio

            _state["loop"] = asyncio.get_running_loop()
        emit("key", action if action != "text" else f"text:{data}")
        return orig_dispatch(self, action, data)

    TuiSession._dispatch = dispatch

    threading.Thread(target=watchdog, name="watchdog", daemon=True).start()


def main():
    emit("start", repr(sys.argv))
    try:
        install()
    except Exception:  # noqa: BLE001
        emit("install-failed", traceback.format_exc().replace("\n", " | "))
    from meshterm.cli import main as cli_main

    sys.argv = ["meshterm"] + sys.argv[1:]
    cli_main()


if __name__ == "__main__":
    main()
