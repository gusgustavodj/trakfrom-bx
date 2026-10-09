# Contributing

Forks and Pull Requests are welcome. Develop on a branch in your fork, explain
the problem and solution, keep the scope focused and include meaningful tests.
Describe contract changes explicitly and name the platforms tested. CI, review
and human maintainer approval precede merge. No PR is auto-merged. Acceptance
and official support claims remain the maintainer's decisions.

Submit only work you have authority to contribute under the project's MIT license.
Do not include proprietary or private material without authorization and recorded
provenance. Never submit real Rekordbox databases, music, real user libraries,
personal paths, secrets, manufacturer binaries/assets or copied proprietary
documentation. Use original code, neutral contracts and synthetic fixtures;
record the origin, license and purpose of any proposed external dependency.
Do not add a new dependency without review.

The source USB must remain read-only; audio stays there. Do not edit
`collection.nml`, weaken storage ownership checks or bypass safety to make a test
pass. A destructive operation proposal needs explicit justification and tests
showing its authorization boundary. Windows regressions must be considered.
Run the commands in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Official project and forks

The official TRAKFROM-BX repository is maintained at
https://github.com/gusgustavodj/trakfrom-bx.
Forks and derivative works are permitted under the MIT License. You may copy,
modify, distribute and sublicense the software subject to [LICENSE](LICENSE),
including preservation of its copyright and permission notice.
Modified versions must not represent themselves as official TRAKFROM-BX releases
or imply endorsement by the maintainer; this is an official-identity distinction,
not an additional MIT license restriction.
Changes intended for inclusion in the official repository must be submitted
through a Pull Request and accepted by the maintainer. Fork creation and
MIT-permitted redistribution do not require maintainer approval.

## Rekordbox library formats and proprietary material

The current public implementation does not include OneLibrary / Device Library
Plus support. Contributions adding other library formats are welcome when they
can be implemented, tested and distributed lawfully. This is a contribution
requirement, not a claim that a particular format cannot legally be implemented.

Do not submit proprietary source code, confidential material, unauthorized
third-party implementations, copyrighted database specifications you lack the
right to redistribute, or other non-redistributable material.
The official project will not accept proprietary, confidential, unauthorized or
otherwise non-redistributable contributions, whether they target OneLibrary,
Device Library Plus or any other format.

Use original work and tests, legitimate documentation, lawfully observed
behavior, redistributable sources and material you have the right to contribute.
Briefly identify relevant sources and permissions; working code alone does not
establish contribution rights. A format or feature is not supported merely
because related abstractions, experiments or historical research exist.

## Porting to macOS

macOS contributions are explicitly welcome: studies, abstractions, adapters,
discovery, native publication mechanisms, lifecycle/mounting, permissions,
watchers, installers/launchers, tests and cross-platform corrections.
The maintainer's lack of a Mac does not block evaluating a PR. Prefer shared core
contracts plus platform adapters, rather than platform conditionals spread
through the product. A large refactor is not a prerequisite for contributing.

Include macOS version, Intel/Apple Silicon architecture, Python version, Traktor
version, hardware and USB details where applicable, reproducible steps, actual
results, limitations and sanitized logs. Never hardcode your volumes or personal
paths, redistribute Traktor/Rekordbox components, write to the source USB or
disable safety. Mock-based tests are useful but cannot replace physical evidence
for hardware claims. A single successful setup does not establish universal support.

Distinguish these evidence labels in the PR:

| Label | Meaning |
| --- | --- |
| `AUTOMATED_TESTED` | Named automated tests passed in the reported environment |
| `COMMUNITY_PHYSICALLY_VALIDATED` | A contributor reports a reproducible physical run |
| `MAINTAINER_PHYSICALLY_VALIDATED` | The maintainer independently performed the physical run |

An accepted port can become `COMMUNITY_VALIDATED` after review of reproducible
evidence; this never implies maintainer physical validation. Official platform
support requires an explicit maintainer decision against agreed criteria:
read-only source behavior, safe ownership/elevation, startup/hotplug/multi-source,
publication, host integration, playback-path handling, shutdown/recovery and
documented limitations on the supported OS/host/hardware matrix. Agree those
criteria in the proposal before claiming official macOS support.

## Other useful contributions

Platform boundaries, safe additional library formats, packaging, diagnostics,
automated tests, documentation and reproducible hardware validation are welcome.
Linux is not currently targeted; architectural proposals/community work can still
be evaluated without making an official support claim.
