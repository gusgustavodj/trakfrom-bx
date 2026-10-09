# Platform boundary

The present layout is flat; this readiness change does not move runtime code or
implement a speculative macOS backend. Future ports should keep contracts shared
and supply adapters at the existing injection points.

| Boundary | Current modules / responsibilities |
| --- | --- |
| Portable core | models, library_models, library_provider/registry, legacy_reader/compatibility, metadata_normalization, key_converter, library_projection, virtual_projection/builder, virtual_nml_catalog, nml_writer |
| Local catalog services | library_service, usb_nml_publication, nml_catalog_materializer; filesystem/output and source safety contracts still apply |
| Windows adapters | windows_drives, windows_virtual_disk_backend, windows_diskpart_virtual_disk_backend, windows_vhdx_nml_adapter, windows_tray, WindowsTraktorProcessProbe in host_process |
| Windows composition | public_runtime, public_watch, public_launcher, public_cli; public runtime defaults to requiring Windows |
| Safety boundary | source_write_guard and vhdx_ownership currently include Win32 physical identity checks; these are not portable hardware implementations |
| Watch boundary | usb_watch contains shared orchestration and Windows volume observation; a future native observer must preserve its lifecycle contract |

PublicRuntime injects volume enumeration, capability/backend factories and host
probing. Provider selection is explicit; the public composition registers only
LegacyServiceProvider. Device Library Plus detection does not install a provider.
Future community adapters need discovery, native publication/mount lifecycle,
permission/elevation, watcher, host probing and launcher/installer implementations.
Preserve USB read-only, no audio copying/cache requirement, exact owned-volume
authorization and conservative cleanup. Document any changed contract in the PR.

The conceptual direction is shared core plus platform/windows, future
platform/macos and other adapters. Physical reorganization is deferred to a scoped
proposal justified by implementation needs. CI tests explicitly classified core
files and does not infer platform support from imports or mocked storage.

## Desktop preferences

`desktop_preferences.py` defines the portable schema: `run_on_startup` defaults
to false and `shutdown_when_traktor_closes` defaults to true. The Windows
adapter `windows_startup.py` uses per-user logon registration; the tray
persists changes and the worker reads shutdown choices during execution.
Manual exit and parent loss still require a confirmed closed host.
The Windows scripts register Start links and a scoped uninstaller.
macOS logon/menu bar/uninstall effects require a future platform adapter;
the shared schema alone does not establish a working macOS installer.
