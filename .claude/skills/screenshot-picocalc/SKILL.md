---
name: screenshot-picocalc
description: Take a screenshot of the PicoCalc's screen remotely over SSH and save it as a PNG in the Downloads folder. Use when JP asks to screenshot, grab, capture, or snap the PicoCalc (or "the pico") screen, wants a picture of what the device is showing for docs, Discord, or an issue, or asks what is on the PicoCalc's screen right now.
---

# Screenshot the PicoCalc

Grabs exactly what the PicoCalc's panel is showing and saves it as a PNG in JP's Downloads
folder. It reads the device's framebuffer (`/dev/fb0`) over SSH, so it captures whatever is
on the console (MeshTerm, a login prompt, a shell) without installing anything on the
device or disturbing what is running there.

## Taking the shot

From the repo root (any Python 3.8+ works; the script uses only the standard library):

```sh
python .claude/skills/screenshot-picocalc/scripts/picocalc_shot.py
```

The last line it prints is the saved file, for example
`C:\Users\…\Downloads\picocalc-2026-10-01-153012.png`. **Then Read that PNG** so you can
see it: confirm it shows what JP expected before reporting back, and tell JP the path.

Options:

- `--scale N`: whole-pixel upscale. The default is 2 (640×640), which stays crisp when
  shared; `--scale 1` keeps the native 320×320.
- `--out PATH`: save somewhere else. A `.png` path is used as the file name; anything
  else is treated as a folder.
- `--rotate 90|180|270`: turn the image clockwise, in case the framebuffer turns out to be
  mounted sideways relative to the panel.

The screenshot is of whatever is on screen *now*, so if JP wants a particular MeshTerm
screen, they navigate to it on the device first.

## Reaching the device

The host, user, key, and password come from `.dev.env` at the repo root (gitignored;
`.dev.env.example` shows the shape): `DEV_PICOCALC_HOST`, `DEV_PICOCALC_USER`,
`DEV_PICOCALC_SSH_KEY`, `DEV_PICOCALC_PASSWORD`. A `DEV_*` environment variable of the same
name wins over the file. Never paste these values into the skill or a commit.

## When it goes wrong

- **"can't reach … over SSH"**: the PicoCalc is off, asleep, or off Wi-Fi. Its Wi-Fi
  takes about 36 seconds after boot to come up and can drop for minutes at a time, so a
  single failure is not a verdict. Ask JP to check the device, then try again.
- **The device side fails with a `sudo` error**: the login user can't read `/dev/fb0`, so
  the script fell back to `sudo` with `DEV_PICOCALC_PASSWORD`, and that failed. The
  permanent fix is a one-time membership in the `video` group, made by editing
  `/etc/group` (BusyBox's `addgroup` misparses), run on the device by JP:

  ```sh
  echo "$DEV_PICOCALC_PASSWORD" | sudo -S sed -i '/^video:/{/:$/!s/$/,/;s/$/'"$DEV_PICOCALC_USER"'/}' /etc/group
  ```

  That appends the user to the `video` line, with a comma only if it already has members.
  Check it with `grep ^video: /etc/group`. It takes effect at the next SSH login.
- **"the frame is entirely black"**: the console has blanked. Ask JP to press a key on the
  device, then take the shot again.
- **The colours look wrong**: the script reads the colour layout from the framebuffer
  driver rather than assuming it, so this would be a driver reporting the wrong layout.
  Compare against the panel before changing anything.

The checkerboard flicker in the panel's top two rows will not appear in the screenshot.
That is an electrical artefact of the panel, not something in the framebuffer, so the
screenshot is cleaner than the device itself. That is expected; don't try to reproduce it.

**Not yet run against the device**: the script was written while the PicoCalc was
offline and tested only against a simulated framebuffer. On the first real run, check the
orientation and the colours against the panel, then delete this note.

## How it works

`scripts/picocalc_shot.py` sends a short Python program to the device, base64-encoded so
no shell quoting can mangle it. On the device, that program asks the framebuffer driver for
the visible resolution, the panning offset, and where each colour channel sits in a pixel
(`FBIOGET_VSCREENINFO`), then reads only the visible rows and writes back one JSON header
line followed by the raw pixels. On the desktop, the script turns those into an RGB PNG
and saves it to the real Downloads folder (asked from Windows, since that folder can be
moved).

For a *text* snapshot of the console instead, which is useful for diffing two renders,
the `profile-picocalc` skill's `drive_console.py` reads `/dev/vcs1`.
