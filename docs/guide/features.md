# What MeshTerm does

This page lists each screen that the menu has, and the command that does the same job
without the menu. It is the catalogue. [The command line](../cli/README.md) is the manual
for the scripted half.

The main menu asks one question: "What would you like to do?" It has six sections. The first
three sections name an action: Message, Watch, and Explore. The last three sections name
whose setting it is. "This node" is the radio in your hand. "Other nodes" are radios of
other persons, which you reach over the mesh. "This app" is the program in front of you.

This page lists each interactive screen with its scripted equivalent, where one exists.

## 💬 Message: the people on the other end

| Feature | What it does | Scripted |
| --- | --- | --- |
| **💬 Chat** | Live full-screen messages on channels and to contacts. The transcript scrolls, the input line stays at the bottom, and sent and received messages appear together. The rooms that you joined are also here. Each post shows under the name of its author. MeshTerm stores each message. Unread counts show in the menu header. | `meshterm chat send / history / list / listen` |
| **📻 Channels** | Create, join, reorder, mute, and share mesh channels. You can share a channel with a QR code or a `meshcore://` link. If you give a channel a **send scope**, its messages are flooded into one region. Only the repeaters that carry that region relay them. When you change to a new device, export all the channels to a file, then import the file on the new device. The **channel guide** lists the public channels that MeshTerm heard, also the channels that are not on your device, and adds one with Enter. | `meshterm channels list / add / join / import / share / clear / scope / export / guide` |
| **📌 Rooms** | Each room server that your radio knows. A room server is a message board on a radio. It keeps the latest posts and sends you the posts that you missed. Join a room with its password. If a room does not answer, MeshTerm tells you what it knows and offers the next step to try. For example, it can send again by flood when the route that your radio learned is no longer good. | `meshterm rooms list / join / forget` |
| **📨 Courier** | A store-and-forward outbox for contacts that you cannot reach yet. Queue a message. MeshTerm sends it when the contact is next heard, or at a time that you set. It tracks the acks, and it waits longer between each try (exponential backoff). | `meshterm courier queue / list / send / cancel / clear` |
| **👥 Contacts** | This node and its known contacts. The screen shows a heat map of how recently each contact was heard, the counts of overheard packets, and the full public keys. The hash of each key is highlighted. The scripted listing shows only the contacts. For this node, run `meshterm info`, which gives much more detail. | `meshterm contacts` |

## 📊 Watch: what the mesh is doing, and what it did

| Feature | What it does | Scripted |
| --- | --- | --- |
| **📊 Dashboard** | The live overview of the mesh. It has a two-hour chart of the activity of all packets with a pulse line, the traffic counts of the session for each packet class, and window RF health next to the live numbers of the radio. The screen updates each second. | none |
| **📰 Live feed** | Each packet as it arrives, newest first. A row shows the time, the type of the packet (its parsed payload class), and what the packet is about. For an advert, that is the node. For a channel text, it is the channel. For a direct message or a request, it is sender → recipient. For a trace, it is the tag. The row also shows the reception quality and, for a scoped flood, the region of the flood. Press Enter to open a row in the packet viewer, with its route graph. | none |
| **🚨 Watchtower** | A passive watcher of the nodes that you star. It gives a silence alarm when a watched node goes quiet. It warns you when the SNR of a node gets worse, and it tells you when the node returns. It also tells you when a node is heard for the first time. It runs in the background during the whole session. Alerts that you did not acknowledge show as a badge in the header. | none |
| **⏳ Time machine** | Everything that the recorder ever heard, as braille charts: reception volume, the median-SNR band, the rhythm of the hours of the day, packets and nodes for each day, and first-ever arrivals. You can switch the time window between 24h, 7d, 30d, and all time. On the PicoCalc, the windows stop at 30d. You can look at one node or at the whole mesh. | none |
| **🎧 Monitor** | The app always records passively. On the command line, this command captures a window of a fixed length in the foreground. It shows each overheard packet as it arrives, and it gives a summary at the end. It transmits nothing. It only listens. | `meshterm monitor --seconds 60` |

## 🧭 Explore: where the nodes are and how they reach each other

| Feature | What it does | Scripted |
| --- | --- | --- |
| **🌍 Map** | The nodes that have a position, plotted on a real OpenStreetMap street map. The map is drawn as Unicode braille (streets, rivers, and place names). You can pan and zoom. Repeaters are highlighted and drawn on top. When you are offline, the map shows a blank grid. | none (a map is a picture, but `meshterm contacts` lists the same nodes) |
| **🌐 Mesh walk** | The observed shape of the mesh, which you walk one node at a time. It is a graph of evidence from trace walks, firmware routes, overheard relay chains, and the neighbour tables of repeaters. It has braille edges that are coloured by SNR, and quality bars. Press Enter to walk to a node. Press ⌫ to go back. Type to find any node. The map shows where the nodes are. The walk shows how the nodes connect. | none |
| **🎯 Trace target** | A live trace screen. Select a target, and each trace streams in, hop by hop. The screen shows the running median and the reliability of each hop. You can compose a route through specific repeaters, or force one. A trace transmits **exactly once**, because repeaters can blacklist nodes that send bursts. To get more samples, run the trace again. | `meshterm trace --target …` |
| **👣 Trace path** | The other half of tracing. You compose the whole circuit by hand, out and back, by the way that you choose. Then you walk it. The path composer suggests each next hop from the links that MeshTerm observed, strongest first. If you have the admin password of a repeater, the composer can also get the neighbour table of that repeater over the mesh. | `meshterm trace-path --path …` |
| **🏆 Trophy case** | MeshTerm scores each trace that comes home on seven boards: longest distance, farthest node, longest single leg, most nodes (with and without revisits), weakest surviving link, and biggest enclosed loop. It keeps the records for each hash width. A record walk must be a trail, which means that no link is crossed twice in the same direction. | `meshterm records` |

## 🔧 This node: the radio in your hand

| Feature | What it does | Scripted |
| --- | --- | --- |
| **📋 Device info** | The identity of the connected companion and its full radio configuration, on one screen. | `meshterm info` |
| **🔧 Device config** | Each setting that the companion firmware has: name, radio, client repeat, auto-add, telemetry, and custom variables. MeshTerm stages your changes for review and then applies them in place. The screen also has operations on the device itself: clock sync, TOML backup and restore, the identity key, reboot, and factory reset. Each operation acts when you confirm it, and the destructive operations need a confirm step. The screen has the same layout as Repeater admin. Thus you configure the radio in your hand and a radio over the mesh in the same way. | `meshterm config` / `config set <key> <value>` / `config backup` / `config reboot` / … |
| **📡 Send advert** | Announce this node to the mesh with a zero-hop advert or a flood advert. You can also share the contact card of this node as a QR code. | `meshterm config advert` / `config share` |
| **🔌 Devices** | This is not a menu screen. You select the companion on the startup splash. The splash lists the serial companions and the Bluetooth LE companions. If only one companion is present, MeshTerm selects it. You can add a network (TCP) companion with host:port. MeshTerm remembers the last good default. If the link drops, MeshTerm detects it at once and offers to reconnect. `meshterm devices` lists what discovery finds. | `meshterm devices` |

## 🗼 Other nodes: the radio of another person, over the mesh

| Feature | What it does | Scripted |
| --- | --- | --- |
| **🗼 Repeater admin** | Set up remote repeaters and room servers over the mesh. First, log in with a remembered password or a password that MeshTerm asks for. Then use an editor in the style of Device config, which speaks the text CLI of the node. It includes the settings that only repeaters have (TX delay, airtime factor, and advert intervals). It also has one-shot actions and a remote command line with readline. A **Regions** page edits the region tree of the repeater: the regions for which it relays floods, and whether it relays unscoped floods. It also edits the home scope and the default scope. | `meshterm repeater-admin <node> <command…>` |
| **🔖 Regions** | This is not a menu screen. The node page of a repeater lists the regions that the repeater carries. MeshTerm asks the repeater one time, without a login. The repeater answers only a neighbour, or a request over a known route. The packet viewer, the message paths, and the live feed show the region of a scoped flood. | `meshterm regions <node>` |
| **📶 TX optimize** | Sweep the transmit power of a remote node while you watch. The sweep has three steps: coarse, then refine, then verify. You see each level as it lands, and you decide whether to apply the best level. The scripted form applies the best level unless you tell it not to. | `meshterm tx-optimize --path … [--no-apply]` |

## ⚙ This app: the program in front of you

This is the last of the three sections. Nothing on the mesh can answer for it. Preferences
is the first row because it is the only row here that changes MeshTerm. The other rows
only describe it. The written pages are at the end.

| Feature | What it does | Scripted |
| --- | --- | --- |
| **⚙ Preferences** | How MeshTerm behaves, in eight groups: Device, Sending, TX optimize, Watchtower, Map, History, Display, and Diagnostics. The page stages your changes, and you save them with one action at the bottom. MeshTerm writes only the values that you changed to `preferences.toml`. [Configuring MeshTerm](configuration.md) has the detail. | `meshterm preferences show / set / reset` |
| **🩺 Diagnostics** | One block of information that each bug report starts with. It shows which build you have and how you installed it, the operating system, the terminal and its size, and the radio and its link. It also shows the verdicts from boot time that no other screen shows (icons, powerline, platform flavour, and the reasons), and how much history MeshTerm stored. It has nothing private: no keys, passwords, positions, or contact names. It describes the mesh only as counts for each table and the span of time that the tables cover. Press `s` to save the block beside the log, to attach to a report. | `meshterm diagnostics [--out PATH]` |
| **📖 About MeshTerm** | What MeshTerm is, where it came from, and the terms of its licence. It includes the map credit, the font credit, and the licence URL. | `meshterm about` |
| **👤 About the author** | The person behind MeshTerm, on the mesh and away from it. | `meshterm about-author` |
| **🔗 Join Discord** | The community server. The page has one invite link and a QR code of the link, which a phone can read. | `meshterm discord` |
| **💰 Support MeshTerm** | What keeps MeshTerm going, and the ways to help it, with money and without money. | `meshterm support` |

## Leaving MeshTerm

**Quit** at the bottom of the main menu leaves MeshTerm, after it asks you to confirm.
**Ctrl+Q** asks the same question from any screen. If you press it again while the question
is open, MeshTerm leaves at once.

You can also hold **Esc**. After one second, a box shows "Hold Esc to quit". A bar in the
box fills during the next two seconds. When the bar is full, MeshTerm closes, in the same
way as with Quit. If you let go before the bar is full, you return to where you were.
While the box is open, MeshTerm does not start to send anything new over the radio. A
message that is already going out finishes first. A quick press of **Esc** still goes back
one screen, as always.

Holding Esc works only where MeshTerm can tell that a key is down. A terminal does not
tell a program when a key comes up. MeshTerm can tell on these systems:

- Windows.
- Linux, where MeshTerm can read the input device of your keyboard. This includes the
  PicoCalc, and a uConsole or other Linux computer where your user is in the `input` group.
- The Cardputer Zero, and the [emulator](../devices/cardputer-zero.md#trying-it-in-the-emulator)
  window for either handheld.

On a Mac, or over SSH, Esc works as it always did.
