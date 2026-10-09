# TRAKFROM-BX

> Private preview / pre-release

An independent, pre-alpha read-only bridge from supported Rekordbox USB libraries
to a managed NML catalog consumed by Traktor. Version: `0.1.0.dev0`.

```text
Rekordbox USB -> TRAKFROM-BX -> managed catalog/NML -> Traktor
Audio stays on the USB.
```

Windows is the maintainer's physically validated platform. The normal desktop
flow detects supported USB sources already connected at startup and monitors
insertion/removal. Multiple sources are represented separately. Playlists and
supported Rekordbox BPM/key metadata are projected into NML. Local VHDX volumes
hold the managed catalog, not copied audio. No mandatory audio cache is used.
The source USB remains read-only; `collection.nml` is never edited directly.

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

## Windows installation and use

The Windows Setup bundles Python; a global Python installation is not required.
Installer source and build instructions are in
[packaging/windows/README.md](packaging/windows/README.md). This private preview
does not publish a GitHub Release or ship Setup.exe in the source repository.
The installer is currently **unsigned**.

When a maintainer-provided Setup is available, run it and accept its installation
UAC prompt. The default destination is Program Files/TRAKFROM-BX. Desktop,
Start Menu and Run on startup are independent choices. Wait for Setup to finish
before launching the application. A manual installation on the maintainer's
Windows machine succeeded; clean-machine USB/Traktor acceptance and Windows 10
physical validation remain pending.

Run on startup uses the current user's Windows logon registration. The storage
worker still requires explicit elevation, so startup currently may require
**recurring UAC**. No elevated scheduled task or persistent service is installed.
For developers, the wheel-based PowerShell installer remains available; that
alternative requires Python 3.11+ and is described in the source scripts.
Uninstall the previous PowerShell installation through its own uninstaller
before switching to Setup.

1. Close running Traktor processes. Start **TRAKFROM-BX** as a normal user.
2. Accept UAC for the storage worker. Wait for initialization to disappear and
   the notification-area status **Pronto; pode abrir o Traktor**.
3. Open Traktor manually with its own shortcut. TRAKFROM-BX does not start it
   automatically or change its settings. Supported USB sources are monitored;
   starting with no USB connected is allowed.
4. Right-click the tray to toggle **Run on startup** and
   **Shutdown when Traktor closes**; both choices persist. Shutdown on close
   defaults to enabled. Keep the launcher/storage worker running during the
   session. With shutdown on close enabled, close Traktor
   normally first. After the host-closed grace period, the worker detaches owned
   VHDX volumes and exits; the tray also disappears. VHDX files remain for reuse.
   Unknown host state or failed detach preserves the volume and reports failure.

With shutdown on close disabled, the application remains in the tray between
Traktor sessions and continues monitoring USB sources. **Encerrar TRAKFROM-BX**
requests a manual exit; cleanup still waits for all Traktor processes to close.

Before updating or uninstalling, close Traktor, request exit if the tray remains,
and wait for its icon to disappear. Use **Uninstall TRAKFROM-BX** in Start or
Windows Installed apps, confirm in its window, and wait for success before
closing that window. Removal is limited to the identified application, its
shortcuts and startup registration. USB, Traktor and local managed catalogs
are preserved. Local sanitized session events and cleanup results are under the managed data
directory's `launcher` folder. Preserve them when diagnosing a failure; sanitize
paths and identifying data before sharing. Do not run the desktop shortcut as
administrator. A pip-only installation does not create shortcuts.

For command-line options after a default Setup installation, open one normal
PowerShell window (adjust the path for a custom destination):

```powershell
& "$env:ProgramFiles\TRAKFROM-BX\runtime\python.exe" -I -B -m rekordbox_usb.public_cli --help
```

Wait for help to finish, then close that window. CLI help is informational;
publication/storage commands still require their normal Windows safety checks.

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

## Development and contributions

Forks are welcome. To propose inclusion in the official project, work on a fork
and branch, then open a Pull Request with tests and platform evidence. Every PR
requires review and a human maintainer decision; no PR is automatically merged.
See [CONTRIBUTING.md](CONTRIBUTING.md), [SUPPORT.md](SUPPORT.md),
[SECURITY.md](SECURITY.md) and [the platform boundary](docs/PLATFORM_ARCHITECTURE.md).
Build/test commands are in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

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

## License and independence

[MIT](LICENSE) applies to content the project has authority to license. No rights
to third-party intellectual property are granted. TRAKFROM-BX is an independent project. It is not affiliated with, sponsored by,
approved by or endorsed by Native Instruments, AlphaTheta, Pioneer DJ or
rekordbox. Trademarks belong to their respective owners. Product names identify
interoperability targets; no manufacturer assets are distributed.
