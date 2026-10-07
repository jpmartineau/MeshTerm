#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Find why the map of MeshTerm has no basemap, on the machine that has the problem.

The map is the only part of MeshTerm that uses the network. It downloads OpenStreetMap
vector tiles over HTTPS from the source that the ``basemap_tilejson_url`` preference names.
MeshTerm catches each failure on that path and logs it at DEBUG
(:mod:`meshterm.services.basemap`). The failures include a missing certificate store, DNS,
a timeout, and a middlebox. The default of ``log_level`` is WARNING. Thus a user whose tiles
never arrive sees a blank ground and no explanation anywhere.

This script gives that explanation. It is standalone and uses only the standard library, so
the user can paste it onto any machine that runs any version of MeshTerm. It prints one
verdict with the fix.

The script must run under the own interpreter of MeshTerm. This is the important point. A
Mac usually has three Pythons, and only one of them is the Python of the app. Only the
certificate store of that Python is the store that the map uses. If another Python starts
the script, the script finds the installed ``meshterm`` command and reads the interpreter
from its shebang. Then it hands itself over to that interpreter. Thus the user does not
need to know the Python that they installed into. Save the script to a file. Do not pipe it
into ``python3 -``, because with no file on disk the script has nothing to hand over.

The decisive test is an A/B. ``curl`` verifies against the trust store of the operating
system, and Python verifies against its own store. If curl succeeds where Python fails, the
certificates are the fault and the network is not. This is the only comparison that
separates two causes that look the same from inside the app.
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

#: The place that tiles come from, unless the preference says otherwise. Change it by hand
#: when ``basemap.DEFAULT_TILEJSON_URL`` changes. This script can run for an install whose
#: ``meshterm`` it cannot import, so it cannot read the constant that it copies.
FALLBACK_TILEJSON_URL = "https://tiles.openfreemap.org/planet"

#: The timeout of each request. It is the same as the timeout of ``BasemapSource``. Thus a
#: link that is only slow fails here for the same reason that it fails in the app, and not
#: for a stricter reason.
TIMEOUT = 12.0

#: The user agent that the app sends. Thus a source that filters by agent treats this run
#: in the same way.
USER_AGENT = "MeshTerm-basemap-doctor (+https://github.com/jpmartineau/MeshTerm)"

#: A tile that exists: zoom 9 over central Poland. A TileJSON that resolves is only half
#: of the path, because the tiles are often on a different host. Thus the check downloads
#: real geometry before it reports success.
SAMPLE_TILE = (9, 285, 167)


def heading(text: str) -> None:
    """Print a section heading."""
    print(f"\n=== {text} ===")


def line(label: str, value: object) -> None:
    """Print one aligned ``label: value`` fact."""
    print(f"  {label:<20} {value}")


def find_meshterm() -> tuple[str | None, str | None, str | None]:
    """Report the MeshTerm version, tile URL, and config directory, if this interpreter has them.

    Returns:
        ``(version, tilejson_url, config_dir)``. Each value is ``None`` when it cannot be read.
    """
    try:
        import meshterm
        from meshterm.core.config import default_config_dir
        from meshterm.core.preferences import Preferences
    except Exception:  # noqa: BLE001 - an import failure means that this is not the interpreter
        return None, None, None
    config_dir = default_config_dir()
    url = None
    try:
        url = Preferences.load(config_dir / "preferences.toml").basemap_tilejson_url
    except Exception:  # noqa: BLE001 - a preferences file that MeshTerm cannot read is a finding
        pass
    return getattr(meshterm, "__version__", "?"), url, str(config_dir)


#: The script sets this on the child that it runs again. Thus a second interpreter that
#: still cannot import MeshTerm continues with the diagnosis. It does not hand itself over
#: again and again.
REEXEC_ENV = "MESHTERM_DOCTOR_REEXEC"


def meshterm_interpreter() -> str | None:
    """The interpreter that runs the installed ``meshterm`` command, if the script can read it.

    On Unix, a console script is a text file. Its shebang names the exact interpreter that
    the app was installed into. The certificate store of that interpreter is the store that
    the map uses, and no other Python on the machine can answer for it. On Windows, the
    console script is a binary launcher with no shebang. Thus this function finds nothing,
    and the diagnosis continues with a banner that names the interpreter that it used.
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
    # A shebang can have arguments (``#!/usr/bin/env python3``). The interpreter is the
    # first word. An ``env`` line does not name an absolute path that is worth a second run.
    interpreter = first[2:].decode("utf-8", "replace").strip().split(" ")[0]
    return interpreter if interpreter and Path(interpreter).is_file() else None


def reexec_under_meshterm() -> None:
    """Hand this script to the interpreter of MeshTerm, if another interpreter runs it.

    The user does not need to know which of the three Pythons the app was installed into.
    This detail is the cause of the failure.
    """
    if os.environ.get(REEXEC_ENV):
        return
    try:
        if importlib.util.find_spec("meshterm") is not None:
            return
    except (ImportError, ValueError):  # a broken or hidden package: continue the search
        pass
    target = meshterm_interpreter()
    here = Path(globals().get("__file__", "")).resolve() if globals().get("__file__") else None
    if target is None or here is None or not here.is_file():
        return
    if Path(target).resolve() == Path(sys.executable).resolve():
        return
    print(f"  (handing over to MeshTerm's own interpreter: {target})")
    # The child uses the same stdout as this process. Thus the buffer of the parent must go
    # out first. If it does not, the banner appears after the report that it introduces.
    # Block buffering makes this certain when the output goes into a file or a paste, and
    # this is the intended use. (``os.execve`` avoids the extra process, but on Windows it
    # is not a real exec. It starts a new process and exits, and it crashes with a segfault
    # under a redirect.)
    sys.stdout.flush()
    child = subprocess.run([target, str(here), *sys.argv[1:]], env={**os.environ, REEXEC_ENV: "1"})
    sys.exit(child.returncode)


def exists_marker(path: str) -> str:
    """Return a marker that shows whether ``path`` is on the disk."""
    return "" if Path(path).exists() else "   <- MISSING"


def describe_env() -> tuple[str | None, str | None]:
    """Print facts about the interpreter, the platform, and MeshTerm.

    Returns:
        The tile URL and the config directory.
    """
    heading("This machine")
    line("Python", sys.version.split()[0])
    line("Interpreter", sys.executable)
    line("OS", f"{platform.system()} {platform.release()} ({platform.machine()})")
    line("TERM", os.environ.get("TERM", "(unset)"))
    # This is not a question about tiles. But it is the other cause that makes a map look
    # wrong, and the answer costs nothing in the same round trip. No COLORTERM means 256
    # colours, and the greens and blues of the basemap collide when they are quantized.
    # Windows is an exception. The Windows output of prompt_toolkit reports 24-bit colour
    # directly, so an unset COLORTERM there shows nothing. On all other platforms, it is
    # all of the evidence.
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
    """Print the certificate store that this Python uses to verify a tile server."""
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


#: The ``curl`` exit codes that mean a TLS failure, not a failure before TLS. The reason to
#: ask curl is that it verifies against the trust store of the operating system. Thus
#: "curl got a certificate that it rejected" and "curl could not reach the host" are
#: opposite answers. Do not combine them into "curl failed".
CURL_TLS_CODES = frozenset({35, 51, 58, 59, 60, 66, 77, 83, 90, 91})


class Curl(NamedTuple):
    """The result that the system ``curl`` gave for the same URL.

    Attributes:
        verdict: ``ok`` (the server answered, with any status), ``tls`` (a certificate or
            handshake failure), ``unreachable`` (curl did not reach the server), or
            ``absent`` (this machine has no ``curl`` to ask).
        detail: The status or error, for the transcript.
    """

    verdict: str
    detail: str


def curl_says(url: str) -> Curl:
    """Download ``url`` with the system ``curl``, which uses the trust store of the OS."""
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
        # Each HTTP status shows that the transport worked. This is the only thing that this
        # comparison asks. A 301 or a 404 also proves TLS and the route.
        return Curl("ok", f"HTTP {proc.stdout.strip()}")
    kind = "tls" if proc.returncode in CURL_TLS_CODES else "unreachable"
    return Curl(kind, f"exit {proc.returncode}: {proc.stderr.strip() or 'no detail'}")


def fetch(url: str) -> tuple[bool, object]:
    """GET ``url`` in the same way as the app. Returns ``(ok, body-or-exception)``."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return True, resp.read()
    except Exception as exc:  # noqa: BLE001 - this function must classify the failure
        return False, exc


#: What to do when this Python cannot verify with its certificate store. The macOS case is
#: first, because it affects a user who did nothing wrong. A python.org install has an
#: empty store until the user runs its own installer command.
CERT_FIX = (
    "Give this Python a certificate store. On macOS a python.org install ships\n"
    "    without one until you run its own command:\n"
    "        open /Applications/Python*/Install\\ Certificates.command\n"
    "    Or point it at certifi's, in the environment MeshTerm runs in:\n"
    "        pip install certifi\n"
    '        export SSL_CERT_FILE="$(python3 -m certifi)"      # then relaunch MeshTerm'
)

#: What to do when TLS ends in the middle of the path and not at the tile server.
INTERCEPT_FIX = (
    "Trust the interceptor's root certificate in this Python (SSL_CERT_FILE can\n"
    "    point at a bundle that includes it), or exempt the tile host from HTTPS\n"
    "    inspection. MeshTerm has no way to accept a certificate it cannot verify."
)


def classify(exc: BaseException) -> tuple[str, str, str]:
    """Change an exception from a download to ``(kind, what happened, what to do about it)``.

    The verdict uses the kind in its reasoning. A certificate failure and a timeout point to
    opposite causes, and the ``curl`` comparison has a different meaning for each.
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
    """Print one coherent story from the result of Python and the result of ``curl``.

    The function must reconcile the two results. It must not print them side by side. If
    Python rejects a certificate and curl accepts it, the fault is the trust store of
    MeshTerm. If both reject it, the network signs the traffic again, or the server is bad.
    The Python exception is the same, but the causes and the fixes are opposite.
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
    """Print the number of tiles that are already on the disk.

    The map can draw these tiles with no network.
    """
    if not config_dir:
        return
    tiles = Path(config_dir) / "tilecache" / "tiles"
    if not tiles.exists():
        line("Tile cache", "empty - no tile has ever arrived")
        return
    line("Tile cache", f"{sum(1 for _ in tiles.rglob('*.pbf'))} tiles at {tiles.parent}")


def main() -> int:
    """Run each check and print one verdict. Returns an exit status for the process."""
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
