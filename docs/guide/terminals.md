# Terminals, icons, and the Windows console

This page explains why MeshTerm looks correct in some terminals and draws empty boxes in
others. It also tells what MeshTerm does about this on Windows, and how to turn that off.

MeshTerm draws emoji icons, braille charts, and powerline path chips. Your terminal decides
if you see them. The font is less important. When a modern terminal must draw a character
that its font does not have, it takes the character from another font on the machine. Thus
the app looks correct in Windows Terminal, in the terminal of VS Code, and on macOS and
Linux. This is usually true also when the font has almost none of these characters.

The Terminal app of macOS is the exception. The charts can be misaligned there. No Mac
monospace font has braille, so the terminal takes it from a proportional font, at the wrong
width.

The classic Windows console does not take characters from other fonts. This is the black
`cmd.exe` window that a double-click opens on Windows 10. (Windows 11, version 22H2 and
later, opens Windows Terminal by default.) The console draws what its one font has, and
empty boxes for all other characters. No font can correct the icons there. Only two fonts
on a Windows machine have emoji, and both are proportional. A console does not accept a
proportional font.

When MeshTerm starts in this console, it **moves to Windows Terminal**. It tells you, and
it opens the app there. MeshTerm does not install or change anything. You get one window
instead of another, and the whole app looks exactly as the screenshots show. Windows
Terminal is already on each Windows 11 machine. On Windows 10 it is a free install.

Sometimes there is no Windows Terminal to move to, and the font of the console cannot
draw the charts. Then MeshTerm offers the next best choice: the charts and marks, without
the icons. MeshTerm includes
[Cascadia Mono PL](https://github.com/microsoft/cascadia-code), the console font of
Microsoft. It is one of the very few monospace fonts that have braille. MeshTerm can
install it for your user account only. This needs no administrator rights and no download. If you
already have a font that can draw the charts (Cascadia, or another such as Iosevka),
MeshTerm offers to switch to that font instead. In each case, MeshTerm asks first
(`[y/N]`), and the default answer is no.

To keep the console that you have, turn all of this off. Go to Preferences → Display →
Console setup.

[Configuring MeshTerm](configuration.md) tells where MeshTerm keeps its state, and how to
point a trial run to another place.
