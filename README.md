# TRAKFROM-BX

> Windows preview / pre-release

## Windows installer included in the source ZIP

### Download ZIP, extract it, run TRAKFROM-BX-Setup.exe

Click **Code -> Download ZIP** on this repository and extract the archive.
**TRAKFROM-BX-Setup.exe is included at the top level of the extracted
trakfrom-bx-main folder, alongside this README and the source code.**
Run that EXE to install the Windows preview. No separate download or Python
installation is required.

[Installer in the repository](./TRAKFROM-BX-Setup.exe)
| [Preview release notes](https://github.com/gusgustavodj/trakfrom-bx/releases/tag/v0.1.0-preview.1)

This unsigned preview Setup was built on 2026-10-10 (build 2026101005).
It includes a compact startup window and a generic Windows tray icon, and supports starting TRAKFROM-BX
while Traktor is already running. Keep the USB connected during playback.
This build preserves artist names from supported USB libraries in generated NML playlists.
To refresh an existing catalog, close Traktor, exit TRAKFROM-BX, install this update,
then start TRAKFROM-BX with the USB connected and wait for publication before reopening Traktor.

After installing or upgrading, the program folder (normally
`C:\Program Files\TRAKFROM-BX`) contains **TRAKFROM-BX.exe** to start the
application and **Uninstall-TRAKFROM-BX.exe** to open the registered uninstaller.
Run the new Setup to update an installation made with the previous preview.

Installer SHA-256:
`24f5f986fee8d6f2158528b0ad92c7451cb99526b55dddcb4e2e4bc426135006`

---

## Screenshots

Screenshots supplied by the maintainer on 2026-10-10, showing the Windows and Traktor workflow. Drive letters and playlist contents are examples from that session. The originals are included in the repository ZIP under `screenshots/`; the gallery uses GitHub image attachments for display. For offline viewing, open the PNG files in that folder.

### Starting TRAKFROM-BX

![TRAKFROM-BX startup window awaiting Windows authorization](https://github.com/user-attachments/assets/cd6c703b-7d3c-40b8-8b5e-20f9b7860a5a)

### Virtual catalog and original USB

![Windows Explorer showing the TRAKFROM_BX virtual volume and the original USB separately](https://github.com/user-attachments/assets/32ebacdb-4fea-49fe-b2b8-7852bf105798)

### Playlists in Traktor

![Traktor browser showing the generated Rekordbox USB playlists](https://github.com/user-attachments/assets/12394aaf-3c82-48a7-9d90-cef9a2b23154)

### Track loaded in Traktor

![Traktor with a playlist selected and a track loaded in deck A](https://github.com/user-attachments/assets/5815abbd-8c0a-4f84-9f1c-bd6c404f7f95)

## What is TRAKFROM-BX?

TRAKFROM-BX is an independent, pre-alpha read-only bridge from supported
rekordbox USB libraries to playlists and NML catalogs consumed by Traktor.
Version: `1.0`. Windows is the official maintainer platform.
Audio stays on the original USB; the local managed virtual disk holds catalogs.
**USB hotplug for live DJ workflows:** supported drives already connected when
TRAKFROM-BX starts are detected automatically; drives connected during a session
are detected too, without restarting TRAKFROM-BX.

**Read-only USB:** TRAKFROM-BX reads the supported library and leaves the USB's
music, database and playlists untouched. Generated catalogs and managed disk
files are written locally on the computer; audio remains on the original USB.

## Instructions

### Quick start

The Windows preview installer is included in the repository ZIP as described above.
The normal installation flow is:

1. Click **Code -> Download ZIP**, extract the archive, and locate `TRAKFROM-BX-Setup.exe` alongside this README.
2. Run `TRAKFROM-BX-Setup.exe` and accept the installation UAC prompt.
3. Choose the installation directory.
4. Select the Desktop, Start Menu and Windows startup options you want.
5. Start TRAKFROM-BX as a normal user and accept UAC for its storage worker.
6. Wait until the startup window becomes the TRAKFROM-BX tray icon.
7. Connect a supported rekordbox USB drive if one is not already connected.
8. If Traktor is not already running, open it manually using its own shortcut.
9. Browse the TRAKFROM-BX-managed source and playlists.
10. Load a track normally in Traktor.

No separate Python installation is required. The normal path is
**Code -> Download ZIP -> extract -> TRAKFROM-BX-Setup.exe**.
The GitHub preview Release is an additional distribution channel. Older tagged
source archives reflect their original commit and may not include the EXE;
use the current main branch ZIP for the combined source-and-installer package.

### Updating or removing an installed version

When Setup recognizes an existing native installation, choose:

- **Update / replace program files:** install the new files at the current path, without uninstalling first.
- **Uninstall and install again:** complete the registered uninstall, then continue Setup.
- **Uninstall only:** complete the registered uninstall and close Setup.

Cancelled or incomplete uninstall blocks replacement. Close TRAKFROM-BX before maintenance.
Preferences, generated catalogs, USB and Traktor are preserved.

### Startup registration and permissions

This build migrates an identified orphaned startup command when its old program
folder is absent or empty. An active other installation or unknown command remains
blocked. The desktop launcher runs as the normal user. Run on startup is a current-user
setting and does not require administrator rights; mounting managed virtual disks
still requires approval for the separate storage worker.

### Reinstalling after uninstall

This build accepts an identified obsolete PowerShell uninstall entry when its
program folder is absent or empty. An active old installation still requires
its own uninstall. Setup cleans the identified obsolete entry after successful
installation. Uninstall removes program files, owned shortcuts and installation/
startup registrations while preserving preferences and local catalogs.

### Starting while Traktor is running

Start `TRAKFROM-BX.exe` normally, even with Traktor open. TRAKFROM-BX does not
start another Traktor instance. A cached catalog that requires rewriting while
Traktor is using it waits until Traktor closes; the tray reports pending catalogs.
Host state that cannot be determined remains a startup error. This change was
tested with synthetic host/storage fixtures; real Traktor validation of this
new build remains to be performed.

### From USB to playback

```text
Supported rekordbox USB
        |
        v
TRAKFROM-BX detects the device
        |
        v
Library data is read from the USB
        |
        v
Tracks and playlists are normalized
        |
        v
Traktor-compatible NML/catalog data is generated
        |
        v
A managed virtual disk is published
        |
        v
Traktor sees the playlists
        |
        v
The user loads a track
        |
        v
Traktor reads the audio from the original USB
```

### USB hotplug and startup detection

- **USB connected before TRAKFROM-BX starts:** leave it connected. The initial
  scan detects supported sources; unplugging and reconnecting is unnecessary.
- **USB hotplug while TRAKFROM-BX is running:** connect a supported drive at any
  time. The watcher detects it and publishes its catalog without restarting
  TRAKFROM-BX. Multiple USB sources are managed independently.
- **Hotplug with Traktor already open:** a new source with no existing managed
  VHDX can be published during the Traktor session. Existing active publications
  can be recognized without rewriting them. Reusing or rebuilding an existing
  managed VHDX may require closing Traktor first; unknown host state blocks
  publication for safety.

This helps live DJ workflows by avoiding a TRAKFROM-BX restart to detect a new
USB. Keep the original USB connected during playback and stop playback before
removing it.

### What happens at each step

| Step | What you do | What TRAKFROM-BX does |
| --- | --- | --- |
| Download | Extract the repository ZIP and locate the bundled Setup.exe. | No application process runs yet. |
| Install | Run Setup; choose the destination and shortcut/startup options. | Installs the bundled runtime and application locally, normally under Program Files/TRAKFROM-BX; creates selected shortcuts and current-user startup registration. Installation may show UAC. |
| Start TRAKFROM-BX | Launch the application as a normal user. | Starts the desktop launcher, startup window and tray interface. |
| Privileged storage worker | Accept the worker's UAC prompt; wait for the tray icon. | Starts a separate elevated worker for Windows virtual-disk operations. It does not install a persistent service or elevated scheduled task. |
| USB detection | Connect a supported USB, or leave it connected before launch. | Detects existing sources and monitors insertion/removal. Multiple sources are managed separately. |
| Rekordbox library read | Keep the USB connected. | Reads the supported Legacy Device Library database (`export.pdb`) and relevant library metadata from the USB. It does not modify the source library. |
| Catalog generation | Wait for publication. | Normalizes tracks/playlists and generates Traktor-compatible NML with supported artist/BPM/key metadata and references to original audio paths. |
| Managed virtual disk publication | Leave the worker running. | Creates/reuses a local VHDX and mounts it to expose generated playlists/catalogs to Traktor. NML and a catalog manifest are written to the managed volume; ownership/state records remain local. |
| Open Traktor | If Traktor is not already running, open it after the startup window becomes a tray icon. | Continues USB monitoring and managed-volume lifecycle handling; it does not launch or configure Traktor. |
| Browse playlist | Browse the managed source and select a playlist. | Makes the generated catalog available on the mounted volume. |
| Load track | Load and play normally in Traktor. | Supplies catalog references; Traktor opens the original USB audio file directly. |

The default local data directory is `%LOCALAPPDATA%/RekordboxUSB`:
`managed` holds VHDX files and associated state; `launcher` holds session events
and cleanup results. Preferences and startup registration are local to Windows.
The privileged worker also uses a protected local Windows runtime directory for
transient storage-operation scripts. These writes occur outside the source USB.

### Where does the audio come from?

Traktor reads audio from the **original USB**, using the locations in the generated
NML. TRAKFROM-BX does not copy audio, stream it to Traktor or require an audio
cache. Once Traktor loads a track, TRAKFROM-BX is not in the audio data path;
Traktor and Windows perform playback reads. Keep TRAKFROM-BX running because it
still manages the published volume and USB lifecycle.

### Why is there a virtual disk?

The managed virtual disk gives Traktor a mounted source containing generated
playlists and catalog data. It holds NML/catalog files, not copied music.
The VHDX is stored on the computer and can be reused in later sessions.

### Source-media safety

TRAKFROM-BX treats the rekordbox USB as read-only: it does not write, rename or
remove its database, playlists or audio. It never directly edits Traktor's
`collection.nml`. Traktor may maintain its own collection as part of normal use;
TRAKFROM-BX does not control those Traktor writes. Automatic Track Collection
cleanup is **NOT IMPLEMENTED**: imported entries may remain after a source is gone.
Stop playback before unplugging the USB; removal can interrupt playback and
leave unavailable entries.

### Startup and shutdown behavior

Traktor may already be running. Otherwise, wait for the startup window to become a tray icon before opening it.
Starting without a USB connected is allowed. The tray provides status,
**About TRAKFROM-BX**, **Run on startup** and **Shutdown when Traktor closes**.
The preference choices persist; shutdown when Traktor closes defaults to enabled.
Windows startup uses the current user's logon registration. The storage worker
still requires elevation, so **UAC may recur at each launch**, including logon.
Do not run the desktop shortcut as administrator.

Keep the launcher and storage worker running during a session. Close Traktor
normally first. With shutdown on close enabled, after the host-closed grace
period the worker detaches its owned VHDX volumes and exits; the tray disappears.
VHDX files remain for reuse. Unknown host state or failed detach preserves the
volume and reports failure. With shutdown on close disabled, the tray remains
between Traktor sessions and continues USB monitoring. The tray's exit command
requests shutdown; cleanup still waits for all Traktor processes to close.

## Current support at a glance

| Area | Current status |
| --- | --- |
| Windows | Official maintainer platform; repeated physical musical validation with Traktor Pro 2, 3 and 4 |
| Legacy Device Library / export.pdb | Supported tested subset; invalid/unsupported inputs are refused |
| OneLibrary / Device Library Plus | NOT CURRENTLY SUPPORTED |
| Automatic Traktor Track Collection cleanup | NOT IMPLEMENTED |
| Direct collection.nml mutation | Not performed by TRAKFROM-BX |
| macOS | Community port welcome; not maintainer physically validated |
| Installer | Unsigned Windows preview included at the top level of the repository ZIP |
| Run on Windows startup | May cause recurring UAC for the privileged storage worker |

## Rekordbox library formats

TRAKFROM-BX only claims support for library formats that are implemented and
validated in the distributed runtime. The current public implementation does
not include OneLibrary / Device Library Plus support.

| Format | Current public runtime |
| --- | --- |
| Device Library / `export.pdb` | Supported bounded Legacy subset; synthetic contracts and local Windows evidence |
| OneLibrary / Device Library Plus / `exportLibrary.db` | NOT CURRENTLY SUPPORTED; no public provider |
| Other formats | Unsupported |

Support is limited to the reader's tested subset. Unsupported or invalid inputs
are refused. Format detection or an enum does not establish extraction support.
No universal Rekordbox or Traktor version compatibility is claimed. Maintainer
physical musical validation covers Traktor Pro 2, 3 and 4 on Windows, based on
repeated playback tests confirmed by the maintainer.

Additional library formats may be contributed when they can be implemented,
tested and distributed appropriately. This technical support status is not a
legal conclusion about implementing a particular format. See CONTRIBUTING.md
for the policy on proprietary and non-redistributable material.

## Platform status

| Platform | Status | Evidence |
| --- | --- | --- |
| Windows | `OFFICIAL_MAINTAINER_VALIDATED` | Maintainer physically validated within the stated scope |
| macOS | `EXPERIMENTAL` / community port welcome | No maintainer physical validation or working platform adapter |
| Linux | `NOT_SUPPORTED` / not currently targeted | No official support claim |

macOS contributions are welcome. Ports and Pull Requests are explicitly welcome. The maintainer currently
validates releases on Windows and cannot independently perform physical macOS
validation. Code review, automated tests and reproducible contributor evidence
may justify accepting a port and recording `COMMUNITY_VALIDATED`; they do not
establish `MAINTAINER_PHYSICALLY_VALIDATED`. CI proves only the tested software
contracts, not USB hotplug, mounting, permissions, hardware or Traktor integration.

## Known limitations

- Automatic Traktor Track Collection cleanup is **not implemented**. Imported
  entries can persist after managed volumes disappear; no direct collection
  editing or automatic destructive workaround is provided.
- Audio is read from the USB. Removing it while a track is playing can interrupt
  playback or leave unavailable entries. Watcher removal handling does not make
  unplugging during playback safe. Stop playback before removing the source.
- macOS and Linux have no supported end-to-end runtime. The public Windows
  runtime refuses unsupported platforms; portable unit tests are not a port.
- OneLibrary / Device Library Plus and universal host compatibility are not delivered by
  this version. The installer is unsigned; Windows 10 and fresh-machine physical
  validation remain pending. Run on startup may require recurring UAC.

## Installation

Setup bundles Python and defaults to Program Files/TRAKFROM-BX; a custom local
installation directory can be chosen. Desktop, Start Menu and Run on startup
are independent options. Wait for Setup to finish before launching the app.
The installer is currently **unsigned**. A manual installation on the maintainer's
Windows machine succeeded; clean-machine USB/Traktor acceptance and Windows 10
physical validation remain pending.

Before updating or uninstalling, close Traktor, request exit if the tray remains,
and wait for its icon to disappear. Use **Uninstall TRAKFROM-BX** in Start or
Windows Installed apps, confirm in its window, and wait for success before
closing that window. Removal is limited to the identified application, its
shortcuts and startup registration. USB, Traktor and local managed catalogs
are preserved. Sanitize diagnostic paths and identifying data before sharing.

## Contributing

Forks are welcome. To propose inclusion in the official project, work on a fork
and branch, then open a Pull Request with tests and platform evidence. Every PR
requires review and a human maintainer decision; no PR is automatically merged.
See [CONTRIBUTING.md](CONTRIBUTING.md), [SUPPORT.md](SUPPORT.md),
[SECURITY.md](SECURITY.md) and [the platform boundary](docs/PLATFORM_ARCHITECTURE.md).

Contributions are particularly welcome for macOS, platform adapters, additional
safe library formats, packaging, tests, diagnostics, documentation and reproducible
hardware validation. These are opportunities, not delivery promises.

## Official project and forks

The official TRAKFROM-BX repository is maintained at
https://github.com/gusgustavodj/trakfrom-bx.

Forks and derivative works are permitted under the MIT License, including
copying, modification, distribution and sublicensing, subject to its terms.
Preserve the copyright and permission notice as required by [LICENSE](LICENSE).
Modified versions must not represent themselves as official TRAKFROM-BX releases
or imply endorsement by the maintainer. This distinguishes official identity;
it does not add a restriction to the MIT License.

Changes intended for inclusion in the official repository must be submitted
through a Pull Request and accepted by the maintainer. Maintainer approval is
required for inclusion in the official project, not for creating or distributing
a fork under MIT.

## Architecture and development

Build/test and developer installation instructions are in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). Installer source and packaging
instructions are in [packaging/windows/README.md](packaging/windows/README.md).
Developer source, wheel and command-line workflows are separate from the normal
Windows Setup flow described above.

## License

[MIT](LICENSE) applies to content the project has authority to license. No rights
to third-party intellectual property are granted.

## Independent project

TRAKFROM-BX is an independent project. It is not affiliated with, sponsored by,
approved by or endorsed by Native Instruments, AlphaTheta, Pioneer DJ or
rekordbox. Trademarks belong to their respective owners. Product names identify
interoperability targets; no manufacturer assets are distributed.
