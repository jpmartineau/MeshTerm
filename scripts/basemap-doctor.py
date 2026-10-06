#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Diagnose why MeshTerm's map has no basemap, on the machine that has the problem.

The map is the only networked part of MeshTerm: it fetches OpenStreetMap vector tiles over
HTTPS from the source named by the ``basemap_tilejson_url`` preference. Every failure on
that path — a missing certificate store, DNS, a timeout, a middlebox — is swallowed and
logged at DEBUG (:mod:`meshterm.services.basemap`), and ``log_level`` defaults to WARNING,
so a reader whose tiles never arrive sees a blank ground and no explanation anywhere.

This script is that explanation. It is standalone and stdlib-only so it can be pasted onto
any machine running any version of MeshTerm, and it prints one verdict with the fix.

It runs under MeshTerm's *own* interpreter, which is the one thing that matters here: a Mac
routinely has three Pythons, only one of them is the app's, and only that one's certificate
store is the one the map actually uses. Started under any other, it finds the installed
``meshterm`` command, reads the interpreter out of its shebang and hands itself over — so
the user needs no idea which Python they installed into. Save it to a file rather than
piping it into ``python3 -``: with no file on disk there is nothing to hand over.

The decisive test is an A/B. ``curl`` verifies against the operating system's trust store
and Python verifies against its own, so curl succeeding where Python fails means the
certificates are at fault and nothing about the network is — the one comparison that
separates the two causes that look identical from inside the app.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

#: Where tiles come from unless the preference says otherwise. Kept in step with
#: ``basemap.DEFAULT_TILEJSON_URL`` by hand: this script may run against an install whose
#: ``meshterm`` it cannot import, so it cannot read the constant it is mirroring.
FALLBACK_TILEJSON_URL = "https://tiles.openfreemap.org/planet"

#: Per-request timeout, matching ``BasemapSource``'s own so that a link which is merely
#: slow fails here for the same reason it fails in the app rather than for a stricter one.
TIMEOUT = 12.0

#: What the app sends, so a source that filters by agent treats this run the same way.
USER_AGENT = "MeshTerm-basemap-doctor (+https://github.com/jpmartineau/MeshTerm)"

#: A tile that exists: zoom 9 over central Poland. The TileJSON resolving is only half the
#: path — the tiles themselves are often a different host — so the check fetches real
#: geometry before it reports success.
SAMPLE_TILE = (9, 285, 167)


def heading(text: str) -> None:
    """Print a section heading."""
    print(f"\n=== {text} ===")


def line(label: str, value: object) -> None:
    """Print one aligned ``label: value`` fact."""
    print(f"  {label:<20} {value}")


def find_meshterm() -> tuple[str | None, str | None, str | None]:
    """Report MeshTerm's version, tile URL and config dir, if this interpreter has them.

    Returns:
        ``(version, tilejson_url, config_dir)``, each ``None`` where it could not be read.
    """
    try:
        import meshterm
        from meshterm.core.config import default_config_dir
        from meshterm.core.preferences import Preferences
    except Exception:  # noqa: BLE001 - any import failure means "not this interpreter"
        return None, None, None
    config_dir = default_config_dir()
    url = None
    try:
        url = Preferences.load(config_dir / "preferences.toml").basemap_tilejson_url
    except Exception:  # noqa: BLE001 - an unreadable preferences file is itself a finding
        pass
    return getattr(meshterm, "__version__", "?"), url, str(config_dir)


#: Set on the re-executed child, so a second interpreter that still cannot import MeshTerm
#: gets on with the diagnosis instead of handing itself on forever.
REEXEC_ENV = "MESHTERM_DOCTOR_REEXEC"


def meshterm_interpreter() -> str | None:
    """The interpreter the installed ``meshterm`` command runs under, if it can be read.

    A console script on Unix is a text file whose shebang names the exact interpreter the
    app was installed into — and that interpreter's certificate store is the one the map
    actually uses, which no other Python on the machine can speak for. On Windows the
    console script is a binary launcher with no shebang, so this finds nothing and the
    diagnosis proceeds with a banner saying which interpreter it did use.
    """
    script = shutil.which("meshterm")
    if script is None:
        return None
    try:
        with open(script, "rb") as fh:
            first = fh.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    # A shebang may carry arguments (``#!/usr/bin/env python3``); the interpreter is the
    # first word, and an ``env`` line names no absolute path worth re-execing.
    interpreter = first[2:].decode("utf-8", "replace").strip().split(" ")[0]
    return interpreter if interpreter and Path(interpreter).is_file() else None


def reexec_under_meshterm() -> None:
    """Hand this script to MeshTerm's own interpreter when it is not the one running it.

    The user should not have to know which of their three Pythons the app was installed
    into — that detail is exactly what the failure is made of.
    """
    if os.environ.get(REEXEC_ENV):
        return
    try:
        if importlib.util.find_spec("meshterm") is not None:
            return
    except (ImportError, ValueError):  # a broken or shadowed package: keep looking
        pass
    target = meshterm_interpreter()
    here = Path(globals().get("__file__", "")).resolve() if globals().get("__file__") else None
    if target is None or here is None or not here.is_file():
        return
    if Path(target).resolve() == Path(sys.executable).resolve():
        return
    print(f"  (handing over to MeshTerm's own interpreter: {target})")
    # The child inherits this stdout, so the parent's buffer has to go out first or the
    # banner lands after the report it introduces. Block buffering makes that certain the
    # moment the output is piped into a file or a paste, which is how it is meant to
    # travel. (``os.execve`` would avoid the extra process, but on Windows it is not a
    # real exec: it spawns and exits, and segfaults outright under a redirect.)
    sys.stdout.flush()
    child = subprocess.run([target, str(here), *sys.argv[1:]], env={**os.environ, REEXEC_ENV: "1"})
    sys.exit(child.returncode)


def exists_marker(path: str) -> str:
    """Return a marker saying whether ``path`` is actually on disk."""
    return "" if Path(path).exists() else "   <- MISSING"


def describe_env() -> tuple[str | None, str | None]:
    """Print interpreter, platform and MeshTerm facts; return the tile URL and config dir."""
    heading("This machine")
    line("Python", sys.version.split()[0])
    line("Interpreter", sys.executable)
    line("OS", f"{platform.system()} {platform.release()} ({platform.machine()})")
    line("TERM", os.environ.get("TERM", "(unset)"))
    # Not a tile question, but it is the other thing that makes a map look wrong and it
    # costs nothing to answer in the same round trip: no COLORTERM means 256 colours, and
    # the basemap's greens and blues are what collide when they are quantized.
    # Windows is exempt: prompt_toolkit's Windows output reports 24-bit outright, so an
    # unset COLORTERM there says nothing. Everywhere else it is the whole of the evidence.
    unset = "(unset)" if os.name == "nt" else "(unset)   <- only 256 colours"
    line("COLORTERM", os.environ.get("COLORTERM") or unset)

    version, url, config_dir = find_meshterm()
    if version is None:
        line("MeshTerm", "NOT importable here   <- probably the wrong interpreter")
    else:
        line("MeshTerm", version)
        line("Config dir", config_dir)
    return url, config_dir


def describe_trust() -> None:
    """Print the certificate store this Python would verify a tile server against."""
    heading("Certificate store")
    line("OpenSSL", ssl.OPENSSL_VERSION)
    paths = ssl.get_default_verify_paths()
    for label, value in (("CA file", paths.cafile), ("CA path", paths.capath)):
        line(label, f"{value}{exists_marker(value)}" if value else "(none)")
    try:
        import certifi

        line("certifi", certifi.where())
    except ImportError:
        line("certifi", "not installed")
    for var in (
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
    ):
        if os.environ.get(var):
            line(var, os.environ[var])
    loaded = len(ssl.create_default_context().get_ca_certs())
    line("Certs loaded", loaded if loaded else "0   <- nothing to verify against")


#: ``curl`` exit codes that mean TLS, as opposed to never getting that far. The whole
#: value of asking curl is that it verifies against the *operating system's* trust store,
#: so "curl got a certificate it disliked" and "curl could not reach the host" are
#: opposite answers and must not be collapsed into "curl failed".
CURL_TLS_CODES = frozenset({35, 51, 58, 59, 60, 66, 77, 83, 90, 91})


class Curl(NamedTuple):
    """What the system ``curl`` made of the same URL.

    Attributes:
        verdict: ``ok`` (the server answered, whatever its status), ``tls`` (a certificate
            or handshake failure), ``unreachable`` (never got that far), or ``absent``
            (no ``curl`` on this machine to ask).
        detail: The status or error, for the transcript.
    """

    verdict: str
    detail: str


def curl_says(url: str) -> Curl:
    """Fetch ``url`` with the system ``curl``, which verifies against the OS trust store."""
    curl = shutil.which("curl")
    if curl is None:
        return Curl("absent", "no curl on this machine to compare against")
    argv = [
        curl,
        "-sS",
        "-o",
        os.devnull,
        "-w",
        "%{http_code}",
        "--max-time",
        str(int(TIMEOUT)),
        url,
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT + 5)
    except (subprocess.TimeoutExpired, OSError):
        return Curl("unreachable", "timed out")
    if proc.returncode == 0:
        # Any HTTP status at all means the transport worked, which is the only thing this
        # comparison is asking about - a 301 or a 404 still proves TLS and the route.
        return Curl("ok", f"HTTP {proc.stdout.strip()}")
    kind = "tls" if proc.returncode in CURL_TLS_CODES else "unreachable"
    return Curl(kind, f"exit {proc.returncode}: {proc.stderr.strip() or 'no detail'}")


def fetch(url: str) -> tuple[bool, object]:
    """GET ``url`` exactly the way the app does. Returns ``(ok, body-or-exception)``."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return True, resp.read()
    except Exception as exc:  # noqa: BLE001 - classifying the failure is the whole job
        return False, exc


#: What to do about a certificate store this Python cannot verify with. The macOS case is
#: first because it is the one that reaches a user who did nothing wrong: a python.org
#: install ships with an empty store until its own installer command is run.
CERT_FIX = (
    "Give this Python a certificate store. On macOS a python.org install ships\n"
    "    without one until you run its own command:\n"
    "        open /Applications/Python*/Install\\ Certificates.command\n"
    "    Or point it at certifi's, in the environment MeshTerm runs in:\n"
    "        pip install certifi\n"
    '        export SSL_CERT_FILE="$(python3 -m certifi)"      # then relaunch MeshTerm'
)

#: What to do when TLS is being terminated in the middle rather than at the tile server.
INTERCEPT_FIX = (
    "Trust the interceptor's root certificate in this Python (SSL_CERT_FILE can\n"
    "    point at a bundle that includes it), or exempt the tile host from HTTPS\n"
    "    inspection. MeshTerm has no way to accept a certificate it cannot verify."
)


def classify(exc: BaseException) -> tuple[str, str, str]:
    """Turn a fetch exception into ``(kind, what happened, what to do about it)``.

    The *kind* is what the verdict reasons with: a certificate failure and a timeout point
    at opposite halves of the world, and the ``curl`` comparison means something different
    for each.
    """
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, urllib.error.HTTPError):
        return (
            "http",
            f"the server answered HTTP {exc.code}",
            "The network is fine. A 4xx means the tile URL is wrong (check the "
            "'Map tiles' preference); a 5xx is the tile server itself.",
        )
    if isinstance(reason, ssl.SSLCertVerificationError):
        return ("cert", "this Python could not verify the tile server's certificate", CERT_FIX)
    if isinstance(reason, ssl.SSLError):
        return ("tls", "the TLS handshake failed", INTERCEPT_FIX)
    if isinstance(reason, socket.gaierror):
        return (
            "dns",
            "the tile host does not resolve",
            "DNS. Check the machine is online, and that no DNS filter or ad-blocker "
            "is swallowing the tile host.",
        )
    if isinstance(reason, TimeoutError):
        return (
            "timeout",
            "the connection timed out",
            "The host is unreachable, or something is dropping the traffic silently: "
            "a firewall, or a link too slow for a 12-second timeout.",
        )
    if isinstance(reason, ConnectionResetError):
        return (
            "reset",
            "the connection was reset",
            "Something in the path is cutting the connection - a firewall or a "
            "middlebox rather than the tile server.",
        )
    return ("other", f"of {type(reason).__name__}: {reason}", "")


def verdict(kind: str, cause: str, fix: str, curl: Curl) -> None:
    """Print one coherent story from what Python saw and what ``curl`` saw.

    The two readings have to be reconciled rather than printed side by side: a certificate
    Python rejects *and curl accepts* is MeshTerm's own trust store, while one they both
    reject is the network re-signing traffic or a genuinely bad server - the same Python
    exception, opposite causes, opposite fixes.
    """
    print(f"  The map cannot fetch tiles because {cause}.")
    if kind in ("cert", "tls"):
        if curl.verdict == "ok":
            print("  The system curl reached that very URL, verifying against the OS trust")
            print("  store, so the network, the ISP and the tile server are all innocent:")
            print("  the fault is this Python's own certificate store.")
            print(f"\n  Fix: {CERT_FIX}")
            return
        if curl.verdict == "tls":
            print("  The system curl refuses the same certificate, so this is not just")
            print("  MeshTerm's trust store: either something on this network is")
            print("  intercepting TLS and re-signing it, or the certificate really is bad.")
            print(f"\n  Fix: {INTERCEPT_FIX}")
            return
        if curl.verdict == "unreachable":
            print("  The system curl could not reach the host at all, so start with the")
            print("  network - a firewall, a proxy or a DNS filter - before the certificates.")
            return
    elif curl.verdict == "ok":
        print("  The system curl reached that very URL, so the route exists and this")
        print("  Python is going somewhere else - check the proxy variables above.")
    elif curl.verdict in ("tls", "unreachable"):
        print("  The system curl could not reach it either, so this is the network path")
        print("  rather than MeshTerm - a firewall, a proxy, a DNS filter or an outage.")
    if fix:
        print(f"\n  Fix: {fix}")


def count_cached(config_dir: str | None) -> None:
    """Print how many tiles are already on disk, which the map can draw with no network."""
    if not config_dir:
        return
    tiles = Path(config_dir) / "tilecache" / "tiles"
    if not tiles.exists():
        line("Tile cache", "empty - no tile has ever arrived")
        return
    line("Tile cache", f"{sum(1 for _ in tiles.rglob('*.pbf'))} tiles at {tiles.parent}")


def main() -> int:
    """Run every check and print one verdict. Returns a process exit status."""
    print("MeshTerm basemap doctor")
    reexec_under_meshterm()
    url, config_dir = describe_env()
    tilejson_url = url or FALLBACK_TILEJSON_URL
    describe_trust()

    heading("Reaching the tile source")
    line("Tile source", tilejson_url + ("" if url else "   (default)"))
    count_cached(config_dir)

    curl = curl_says(tilejson_url)
    line("System curl", curl.detail)

    ok, result = fetch(tilejson_url)
    line("This Python", "OK" if ok else f"FAILED - {type(result).__name__}")

    tile_ok = False
    if ok:
        try:
            template = json.loads(result)["tiles"][0]
            z, x, y = SAMPLE_TILE
            tile_url = template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
            tile_ok, tile_result = fetch(tile_url)
            line("One real tile", f"OK, {len(tile_result)} bytes" if tile_ok else "FAILED")
            if not tile_ok:
                result = tile_result
        except (ValueError, KeyError, IndexError) as exc:
            line("TileJSON", f"unreadable: {exc}")

    heading("Verdict")
    if ok and tile_ok:
        print("  Tiles reach this machine, so the basemap is NOT a network problem.")
        print("  If the map still looks wrong it is the terminal: COLORTERM unset means")
        print("  256 colours, and the basemap's greens and blues collide once they are")
        print("  quantized. Apple's Terminal.app cannot do 24-bit colour at all - try")
        print("  iTerm2, Ghostty or kitty.")
        return 0

    failure = result if isinstance(result, BaseException) else RuntimeError("no answer")
    verdict(*classify(failure), curl)
    prefs = Path(config_dir) / "preferences.toml" if config_dir else Path("~/.meshterm")
    print("\n  Meanwhile the map still works - it plots nodes without a basemap.")
    print("  Per-request detail goes to the log if you set log_level = DEBUG in")
    print(f"  {prefs} and reopen the map.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
