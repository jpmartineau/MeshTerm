# The Android sound theme

These are the sounds of the `android` sound theme. MeshTerm plays them for its
notifications (refer to `meshterm/services/sound.py`).

| File | Event | Source in AOSP `data/sounds` |
| --- | --- | --- |
| `Carme.wav` | Direct message | `notifications/material/ogg/Carme.ogg` |
| `Ariel.wav` | Mention of you | `notifications/material/ogg/Ariel.ogg` |
| `InCallNotification.wav` | Channel message | `effects/ogg/InCallNotification.ogg` |
| `Ceres.wav` | Room post | `notifications/material/ogg/Ceres.ogg` |
| `LowBattery.wav` | Battery low | `effects/ogg/LowBattery.ogg` |
| `Effect_Tick.wav` | Packet tick | `effects/ogg/Effect_Tick.ogg` |

The sources are from the Android Open Source Project, `platform/frameworks/base` at
commit `1cdfff555f4a21f71ccc978290e2e212e2f8b168`. `scripts/sounds.py` makes these files
from them again.

<!-- ste: off -->
Copyright The Android Open Source Project. Licensed under the Apache License,
Version 2.0 (https://www.apache.org/licenses/LICENSE-2.0).

Modified by MeshTerm: each file was decoded from Ogg Vorbis, mixed down to one
channel, and written as 16-bit PCM WAV at its original sample rate.
<!-- ste: on -->
