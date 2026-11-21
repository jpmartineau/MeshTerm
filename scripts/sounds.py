# SPDX-License-Identifier: Apache-2.0
"""Make the WAV files of the Android sound theme from the AOSP sources.

MeshTerm plays its notification sounds through the player of the operating system:
``winsound`` on Windows, ``afplay`` on macOS, and ``pw-play``, ``paplay``, or ``aplay`` on
Linux. All of these players read WAV, but not all of them read Ogg Vorbis. The sources in
AOSP are Ogg Vorbis. Thus this script downloads each source at a fixed AOSP commit, decodes
it, mixes it to one channel, and writes a 16-bit WAV into ``meshterm/assets/sounds/android``.
The WAV files are in the repository, so a build of MeshTerm never runs this script.

The decoder is ``soundfile`` (libsndfile), which is not a dependency of MeshTerm. Install
it in a separate environment first::

    python -m venv /tmp/sounds-venv
    /tmp/sounds-venv/bin/pip install soundfile
    /tmp/sounds-venv/bin/python scripts/sounds.py

The sources are under the Apache License 2.0 (``Android.bp`` in ``data/sounds`` declares
``Android-Apache-2.0``), the same licence as MeshTerm. ``README.md`` in the output folder
names them.
"""

from __future__ import annotations

import base64
import urllib.request
import wave
from pathlib import Path

import numpy as np
import soundfile

#: The AOSP commit of ``platform/frameworks/base`` that the files come from. A fixed commit
#: makes the output the same at each run.
COMMIT = "1cdfff555f4a21f71ccc978290e2e212e2f8b168"

#: Each source, by its path under ``data/sounds`` at :data:`COMMIT`. JP selected them by
#: ear on 2026-11-20, from an audition of 188 sounds.
SOURCES: tuple[str, ...] = (
    "notifications/material/ogg/Carme.ogg",
    "notifications/material/ogg/Ariel.ogg",
    "notifications/material/ogg/Ceres.ogg",
    "effects/ogg/InCallNotification.ogg",
    "effects/ogg/LowBattery.ogg",
    "effects/ogg/Effect_Tick.ogg",
)

_BASE = "https://android.googlesource.com/platform/frameworks/base/+/{commit}/data/sounds/{path}"
_OUT = Path(__file__).resolve().parent.parent / "meshterm" / "assets" / "sounds" / "android"


def download(path: str) -> bytes:
    """Download one source file from Gitiles, which serves a file as Base64 text."""
    url = _BASE.format(commit=COMMIT, path=path) + "?format=TEXT"
    with urllib.request.urlopen(url, timeout=30) as reply:  # noqa: S310 - a fixed https URL.
        return base64.b64decode(reply.read())


def write_wav(target: Path, samples: np.ndarray, rate: int) -> None:
    """Write mono samples (floats from -1 to 1) as a 16-bit PCM WAV file."""
    pcm = np.clip(samples, -1.0, 1.0)
    data = (pcm * 32767).round().astype("<i2").tobytes()
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(data)


def main() -> None:
    """Download, decode, and write each source."""
    _OUT.mkdir(parents=True, exist_ok=True)
    tmp = _OUT / "_source.ogg"
    for path in SOURCES:
        tmp.write_bytes(download(path))
        samples, rate = soundfile.read(str(tmp), dtype="float64", always_2d=True)
        mono = samples.mean(axis=1)
        target = _OUT / (Path(path).stem + ".wav")
        write_wav(target, mono, rate)
        print(f"{target.name}: {len(mono) / rate:.2f} s, {target.stat().st_size} bytes")
    tmp.unlink()


if __name__ == "__main__":
    main()
