# Windows Setup build

The product uses the official CPython Embedded 3.13.16 AMD64 distribution and
Inno Setup 7.1.0. The application has no third-party runtime Python packages.
Build tools and test dependencies are never installed with the product.

1. Verify the positive `PUBLIC_FILES.txt` inventory and run the public suite.
2. Build wheel/sdist from that curated public source, with `SOURCE_DATE_EPOCH`
   fixed and the pinned build tools in `pyproject.toml`. Audit archive membership.
3. Obtain the official Python embedded archive and applicable licenses separately.
   `build_installer.py` checks the fixed archive SHA-256 and exact runtime members.
4. Run `build_installer.py --candidate <source> --wheel <wheel> --python-zip <zip>
   --licenses <license-directory> --stage <new-staging> --compiler <ISCC.exe>
   --native-compiler <cl.exe> --windows-sdk <Windows-Kits/10> --output <dist>
   --assemble-only` with absolute paths. Existing MSVC and Windows SDK compile
   the project's native_frontend.c into two x64 GUI entrypoints, with Windows
   DLL imports only and no bundled CRT or .NET runtime.
5. Test that staging using its private Python, with `-I -B`, before compiling.
   Keep pytest and synthetic fixtures in a separate test copy. Compare all app
   bytes against staging; never ship test tools or add them to production `_pth`.
6. Call `compile_setup` with the staging manifest, or use the same tool without
   `--assemble-only` for an already validated set of source/runtime inputs.
   Compilation consumes an exact generated file list, with no recursive wildcard.
7. Validate the resulting Setup, then publish hashes and the local checkpoint.

Default installation is administrative in Program Files. Custom local folders
are supported. Desktop, Start Menu and per-user logon are independent tasks.
The application and storage worker preserve their existing UAC separation.
There is no elevated scheduled task or persistent Windows service.

DiskPart command files use a separate privileged known-folder location:
ProgramData/TRAKFROM-BX/secure-runtime. Owner, protected DACL, physical identity
and reparse checks are mandatory. Open ancestor and read-only script handles
protect the entire child lifetime. An untrusted pre-existing directory is
rejected; it is never repaired or used as a fallback.

Native uninstall preserves user catalogs, VHDXs and preferences. Upgrade keeps
the installation identity and rejects downgrade, foreign paths and collisions.
Close the application before maintenance. A previous PowerShell installation
must be removed using its own uninstaller before switching to this Setup.

The installed folder contains TRAKFROM-BX.exe and Uninstall-TRAKFROM-BX.exe.
The first starts the bundled isolated Python launcher; the second invokes only
this AppId's registered native uninstaller in the same verified install folder.
Both require a matching installation marker and reject redirected paths.
The uninstaller's files/log remain under Inno's control; they are not renamed.
`/verify` is a read-only integrity check and never starts the app or uninstaller.

TRAKFROM-BX can start while Traktor is already running. New sources can be
published and existing attached catalogs reconciled through existing guards.
An existing detached catalog that needs reuse/rewrite is deferred until the
host closes; the service stays active and reports the pending catalogs.
Unknown host state, source protection, ownership and cleanup remain fail-closed.

Reinstallation checks the old PowerShell registration against its canonical
per-user folder. An identified entry whose folder is absent or empty does not
block Setup. An active installation, redirected path or foreign registration
still blocks. After successful installation, and during uninstall, an identified
obsolete entry with no child records is removed. Uninstall removes installed
application files, owned shortcuts and startup/uninstall registrations; user
preferences and managed catalogs are preserved. Unexpected records are preserved.

When the current installation is identified, interactive Setup offers update /
replace program files, uninstall and install again, or uninstall only. Update
uses the normal Inno upgrade log without uninstalling first. The other choices
run only this installation's registered uninstaller and wait for it. Cancelled
or partial uninstall blocks replacement. Uninstall only closes Setup afterward.

Version 1.0 / build 2026101003 includes author, repository and distribution date
in the displayed MIT license. Exact orphaned native/legacy startup commands
can be migrated after read-only preflight. Unknown or active commands remain
protected; the value is checked again before deletion. The desktop frontend
remains asInvoker; current-user startup configuration requires no elevation.
