/* Authorial Win32 frontends; no shell command, PATH lookup or CRT dependency. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>

#ifndef UNINSTALL_FRONTEND
#define UNINSTALL_FRONTEND 0
#endif
#define CAPACITY 32768
static WCHAR root[CAPACITY], target[CAPACITY], scratch[CAPACITY];
static WCHAR marker[CAPACITY], value[CAPACITY], command[CAPACITY];
static STARTUPINFOW startup;
static PROCESS_INFORMATION process;
static SHELLEXECUTEINFOW shell;

static BOOL append(WCHAR *destination, const WCHAR *suffix) {
    DWORD i = 0, j = 0;
    while (destination[i]) ++i;
    while (suffix[j]) {
        if (i >= CAPACITY - 1) return FALSE;
        destination[i++] = suffix[j++];
    }
    destination[i] = 0;
    return TRUE;
}

static BOOL equal(const WCHAR *a, const WCHAR *b) {
    return CompareStringOrdinal(a, -1, b, -1, TRUE) == CSTR_EQUAL;
}

static BOOL safe_path(const WCHAR *path) {
    DWORD length, attributes;
    scratch[0] = 0;
    if (!append(scratch, path)) return FALSE;
    length = (DWORD)lstrlenW(scratch);
    if (length < 3 || scratch[1] != L':' || scratch[2] != L'\\') return FALSE;
    for (;;) {
        attributes = GetFileAttributesW(scratch);
        if (attributes == INVALID_FILE_ATTRIBUTES ||
            (attributes & FILE_ATTRIBUTE_REPARSE_POINT)) return FALSE;
        if (length <= 3) break;
        while (length > 3 && scratch[length - 1] != L'\\') --length;
        if (length > 3) --length;
        scratch[length] = 0;
    }
    return TRUE;
}

static BOOL installed_root(void) {
    DWORD length = GetModuleFileNameW(NULL, root, CAPACITY);
    if (!length || length >= CAPACITY - 64 || !safe_path(root)) return FALSE;
    while (length && root[length - 1] != L'\\') --length;
    if (length <= 3) return FALSE;
    root[length - 1] = 0;
    if (!append(marker, root) || !append(marker, L"\\install-marker.ini") ||
        !safe_path(marker)) return FALSE;
    GetPrivateProfileStringW(L"installation", L"product", L"", value, CAPACITY, marker);
    if (!equal(value, L"TRAKFROM-BX")) return FALSE;
    GetPrivateProfileStringW(L"installation", L"app_id", L"", value, CAPACITY, marker);
    if (!equal(value, L"{574E3A6C-90F3-489F-A34F-C1D1A3E8609F}")) return FALSE;
    GetPrivateProfileStringW(L"installation", L"schema", L"", value, CAPACITY, marker);
    if (!equal(value, L"1")) return FALSE;
    GetPrivateProfileStringW(L"installation", L"path", L"", value, CAPACITY, marker);
    if (!equal(value, root)) return FALSE;
    GetPrivateProfileStringW(L"installation", L"installation_id", L"", value, CAPACITY, marker);
    return value[0] != 0;
}

#if UNINSTALL_FRONTEND
static BOOL uninstall_target(HKEY hive) {
    HKEY key;
    DWORD bytes = sizeof(value), type, length, root_length = (DWORD)lstrlenW(root);
    const WCHAR *path;
    const WCHAR *name;
    if (RegOpenKeyExW(hive,
        L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
        L"{574E3A6C-90F3-489F-A34F-C1D1A3E8609F}_is1",
        0, KEY_QUERY_VALUE | KEY_WOW64_64KEY, &key) != ERROR_SUCCESS) return FALSE;
    value[CAPACITY - 1] = 0;
    if (RegQueryValueExW(key, L"UninstallString", NULL, &type,
        (BYTE *)value, &bytes) != ERROR_SUCCESS || type != REG_SZ ||
        bytes < sizeof(WCHAR) || bytes > sizeof(value) || bytes % sizeof(WCHAR) ||
        value[bytes / sizeof(WCHAR) - 1]) {
        RegCloseKey(key);
        return FALSE;
    }
    RegCloseKey(key);
    path = value;
    length = (DWORD)lstrlenW(path);
    if (length >= 2 && path[0] == L'"' && path[length - 1] == L'"') {
        value[length - 1] = 0;
        ++path;
        length -= 2;
    }
    if (length != root_length + 1 + 12 || path[root_length] != L'\\' ||
        CompareStringOrdinal(path, (int)root_length, root, (int)root_length, TRUE) != CSTR_EQUAL)
        return FALSE;
    name = path + root_length + 1;
    if (CompareStringOrdinal(name, 5, L"unins", 5, TRUE) != CSTR_EQUAL ||
        name[5] < L'0' || name[5] > L'9' || name[6] < L'0' || name[6] > L'9' ||
        name[7] < L'0' || name[7] > L'9' || !equal(name + 8, L".exe")) return FALSE;
    return append(target, path) && safe_path(target);
}
#endif

void WINAPI entry(void) {
    const WCHAR *arguments = GetCommandLineW();
    BOOL verify;
    DWORD result = 3;
    if (*arguments == L'"') {
        ++arguments;
        while (*arguments && *arguments != L'"') ++arguments;
        if (*arguments) ++arguments;
    } else {
        while (*arguments && *arguments != L' ' && *arguments != L'\t') ++arguments;
    }
    while (*arguments == L' ' || *arguments == L'\t') ++arguments;
    verify = equal(arguments, L"/verify");
    if ((*arguments && !verify) || !installed_root()) goto finished;
#if UNINSTALL_FRONTEND
    if (!uninstall_target(HKEY_LOCAL_MACHINE)) {
        target[0] = 0;
        if (!uninstall_target(HKEY_CURRENT_USER)) goto finished;
    }
#else
    if (!append(target, root) || !append(target, L"\\runtime\\pythonw.exe") ||
        !safe_path(target)) goto finished;
#endif
    if (verify) { result = 0; goto finished; }
#if UNINSTALL_FRONTEND
    shell.cbSize = sizeof(shell);
    shell.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC;
    shell.lpVerb = L"open";
    shell.lpFile = target;
    shell.lpDirectory = root;
    shell.nShow = SW_SHOWNORMAL;
    if (ShellExecuteExW(&shell)) {
        if (shell.hProcess) CloseHandle(shell.hProcess);
        result = 0;
    }
#else
    if (!append(command, L"\"") || !append(command, target) ||
        !append(command, L"\" -I -B -m rekordbox_usb.public_launcher service")) goto finished;
    startup.cb = sizeof(startup);
    if (CreateProcessW(target, command, NULL, NULL, FALSE, 0, NULL, root, &startup, &process)) {
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
        result = 0;
    }
#endif
finished:
    if (result && !verify)
        MessageBoxW(NULL, L"TRAKFROM-BX could not start. Check the installation and use its original folder.",
                    L"TRAKFROM-BX", MB_OK | MB_ICONERROR);
    ExitProcess(result);
}
