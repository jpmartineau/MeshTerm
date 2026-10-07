# Security policy

## Reporting a vulnerability

Use the private vulnerability reporting of GitHub. Go to the **Security** tab of this
repository and choose **Report a vulnerability**. This opens a private thread. Only you and
the maintainer can see it, so nothing is disclosed while we correct the problem.

If you cannot use this for any reason, send an email to <johnputer@meshterm.net> instead.

**Do not open a public issue for a security problem.**

## What to expect

One person maintains MeshTerm in his spare time. For this reason, this policy promises only
what he can keep:

- **An acknowledgement within a week.** This is not a correction within a week. It is an
  answer that says that he read the report and what happens next.
- **A correction or a decision within 90 days** for each confirmed problem. If it will take
  more time, you will be told why.
- **Credit in the release notes**, unless you prefer that we do not use your name.

A slow reply is not a rejection. If you hear nothing for a week, you are welcome to remind
us on the same thread.

## Supported versions

While the major version is `0`, only the **latest release** is supported. We do not make
backports. A correction is in the next version.

## Scope

In scope: anything in this repository.

- Code execution, path traversal, or file overwrite that data which MeshTerm reads can
  cause. This data includes packets overheard from the mesh, the responses of a companion,
  a config file or a preferences file, and a downloaded map tile.
- Anything that discloses a **channel key**, a **repeater admin password**, or the private
  key material of a node. This includes disclosure into the local database, a log, an
  export, or the screen, when the material must not be there.
- Anything that lets a crafted packet corrupt or delete the observation database.
- Weaknesses in how MeshTerm stores credentials on disk.

Out of scope, but we still want you to tell us somewhere:

- Vulnerabilities in the **MeshCore firmware** or in the companion device itself. These
  belong upstream, with the MeshCore project.
- Vulnerabilities in the `meshcore` Python library, `bleak`, or any other dependency.
  Report these to their maintainers. If a change in MeshTerm can reduce the risk of one,
  we want to know.
- The fact that everyone within radio range can hear a public channel. This is how a
  mesh works. It is not a defect.
- Anything that needs an attacker who already has write access to your filesystem or to
  your companion device.

## A note on what MeshTerm handles

MeshTerm decrypts channel traffic with keys that **you** give it. It stores everything that
it overhears in a local SQLite database. This database is not encrypted. Treat it in the
same way as each file that holds the history of your mesh. A backup of the database has the
same importance.
