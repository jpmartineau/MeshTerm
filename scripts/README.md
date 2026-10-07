# scripts/

These are tools for the repository. None of them is in the wheel.

| Script | What it is for |
|---|---|
| `basemap-doctor.py` | Finds why a user's map has no basemap: certificates, DNS, proxy, or colour depth |
| `cardputer-zero/` | `launcher-entry.sh` puts a source checkout in the Cardputer Zero's app launcher. `remove` takes it out. This is for the time before there is a store package. `meshterm.png` is the icon: the website's logo, at the launcher's 100×100 size. `type-text.py` types a pasted string (an API key, a Wi-Fi password) on the Cardputer's keyboard from another computer, over SSH |
| `picocalc-lyra/` | Setup of the Calculinux device: console font, palette, Wi-Fi. `xiao-radio/` is the kit for the XIAO radio (firmware build, flashing, UART setup) |
| `uconsole/` | The uConsole's always-on SPI bridge service. MeshTerm can also control that radio directly, with no script (refer to `docs/devices/uconsole.md`) |

## basemap-doctor.py

The map is the only part of MeshTerm that uses the network. MeshTerm logs each fetch
failure on this path at DEBUG, and `log_level` is WARNING by default. Thus a user whose
tiles do not arrive sees a blank ground and no explanation. This script prints the
explanation and the correction.

To use it, tell the user to run this **on the machine that has the problem**:

```bash
curl -fsSL https://raw.githubusercontent.com/jpmartineau/MeshTerm/main/scripts/basemap-doctor.py -o /tmp/basemap-doctor.py
python3 /tmp/basemap-doctor.py
```

Any `python3` is correct. If that `python3` does not have MeshTerm, the script reads the
interpreter from the installed `meshterm` command's shebang, and it runs again with
that interpreter. Thus the answer always comes from **the app's own Python**. A Mac
often has three Pythons, and only one of them has the trust store that is important. Save
the script to a file, as in the example. Do not pipe it into `python3 -`. When there is no
file on the disk, the script cannot run again with the other interpreter.

Ask the user to paste all the output back to you. The output names the interpreter, the
certificate store, the tile source, the number of cached tiles, and `COLORTERM`. Then it
gives one verdict:

- **curl reached it, Python did not.** The Python's certificate store is empty. A
  python.org install on macOS has an empty store until the user runs
  `Install Certificates.command`.
- **neither reached it.** The problem is in the network path: a firewall, a proxy, a DNS
  filter, or an outage. There is nothing to correct in MeshTerm.
- **both reached it.** The tiles are correct. The problem is colour. When `COLORTERM` is not
  set, the terminal uses 256 colours, and the greens and blues of the basemap are too
  similar. Terminal.app cannot do 24-bit colour.

The exit status is 0 when the tiles arrive and 1 when they do not. Thus a script can use it.
