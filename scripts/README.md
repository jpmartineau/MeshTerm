# scripts/

Repo-side tools. None of these ship in the wheel.

| Script | What it is for |
|---|---|
| `basemap-doctor.py` | Why a user's map has no basemap — certificates, DNS, proxy, or colour depth |
| `picocalc/` | Calculinux device setup: console font, palette, Wi-Fi; `xiao-radio/` is the XIAO radio kit (firmware build, flashing, UART setup) |
| `uconsole/` | The uConsole's always-on SPI bridge service (MeshTerm can also drive that radio directly, with no script at all — see `docs/uconsole.md`) |

## basemap-doctor.py

The map is the only networked part of MeshTerm, and every fetch failure on that path is
logged at DEBUG while `log_level` defaults to WARNING — so a reader whose tiles never
arrive sees a blank ground and no explanation. This prints the explanation and the fix.

Send a user this, to run **on the machine with the problem**:

```bash
curl -fsSL https://raw.githubusercontent.com/jpmartineau/MeshTerm/main/scripts/basemap-doctor.py -o /tmp/basemap-doctor.py
python3 /tmp/basemap-doctor.py
```

Any `python3` will do: if that one has no MeshTerm, the script reads the interpreter out
of the installed `meshterm` command's shebang and hands itself over, so the answer always
comes from **the app's own Python** — a Mac routinely has three and only that one has the
trust store that matters. Save it to a file as above rather than piping into `python3 -`:
with no file on disk there is nothing to hand over.

Have them paste the whole output back. It names the interpreter, the certificate store,
the tile source, the cached-tile count, `COLORTERM`, and then one verdict:

- **curl reached it, Python did not** — the Python's certificate store is empty. A
  python.org install on macOS ships that way until `Install Certificates.command` is run.
- **neither reached it** — the network path: a firewall, a proxy, a DNS filter, or an
  outage. Nothing to fix in MeshTerm.
- **both reached it** — tiles are fine and the complaint is colour: an unset `COLORTERM`
  means 256 colours, where the basemap's greens and blues collide. Terminal.app cannot do
  24-bit colour at all.

Exit status is 0 when tiles arrive and 1 when they do not, so it can be scripted.
