# Development and reproducible builds

Use Python 3.11+. Runtime has no external Python dependencies. Development/test
tools are separate: existing pytest; pinned build 1.6.1, setuptools 83.0.0 and
wheel 0.47.0. CI uses GitHub's checkout/setup-python actions, not vendored code.

On Windows, use one normal PowerShell window in the public source root:

```powershell
Set-Location 'C:\path\to\TRAKFROM-BX'
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install build==1.6.1 setuptools==83.0.0 wheel==0.47.0 pytest==9.1.1
$env:SOURCE_DATE_EPOCH = '1791504000'
& .\.venv\Scripts\python.exe scripts\check_public_tree.py
& .\.venv\Scripts\python.exe scripts\run_public_tests.py --temp-root "$env:TEMP\trakfrom-bx-tests"
& .\.venv\Scripts\python.exe -m build --no-isolation
& .\.venv\Scripts\python.exe scripts\check_artifacts.py dist
& .\.venv\Scripts\python.exe -m pip install --no-index --no-deps --force-reinstall .\dist\trakfrom_bx-1.0-py3-none-any.whl
& .\.venv\Scripts\python.exe scripts\smoke_imports.py --installed
& .\.venv\Scripts\python.exe scripts\run_public_tests.py --installed --temp-root "$env:TEMP\trakfrom-bx-installed-tests"
& .\.venv\Scripts\trakfrom-bx.exe --help
```

Run commands in this order and advance only after each exits successfully. Wait
for pytest's summary/build success; no host, storage worker or Traktor process is
needed for these synthetic tests. After CLI help exits, close the window. Existing
approved local toolchains can be reused offline instead of downloading tools.
Choose fresh temporary directories: pytest may clear its own basetemp.

`--core` on the test/import scripts selects the explicit portable subset. Core
CI uses macOS as a portability signal; it performs no Windows backend execution,
USB mounting or physical host test. The full Windows suite uses synthetic
fixtures and injected backends; real PowerShell tests stub disk cmdlets. No
physical test is included in the public CI suite.

Build twice from identical allowlisted bytes and the same SOURCE_DATE_EPOCH;
compare wheel/sdist SHA-256. The small build wrapper normalizes sdist metadata;
setuptools handles wheel timestamps. The sdist carries the full public source,
test fixtures and contributor/CI files. Build outputs/venvs are not source inputs.
PUBLIC_FILES.txt defines exact public input paths; adding a file needs explicit
inventory review. Check scripts enforce archive membership and source byte parity.
