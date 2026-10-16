"""
Standalone tool for the permissions of a folder tree (Windows; local, network
and long paths). Each step is run by hand, when needed:

    grant    <folder>        Give an account (default: you) Modify on the folder
                             and everything under it. Items that can't be
                             changed go to the error log; --take-ownership takes
                             ownership of those and tries again.
    lockdown <folder>        Reduce every other user/group with write access to
                             read. The account, SYSTEM and Administrators keep
                             their rights; --exclude keeps folders as they are.
    restore  <changes.csv>   Put back the permissions from before a grant or
                             lockdown run, from the changes file it wrote.

Every run writes into --output (default: the current folder):
    permissions_<step>_<timestamp>_changes.csv  every item changed, with its
                                                permissions before and after
    permissions_<step>_<timestamp>_errors.csv   every item that couldn't be
                                                read or changed, with the error
--dry-run writes the changes file (..._dryrun_changes.csv) and changes nothing.

The account is you by default; --account takes a name, email address or SID,
and --account-from the owner of a file/folder. On a network share, the server
may know an account by another ID than this PC (cloud vs. domain account):
--account-from <a folder you created on that share> uses the server's.

Usage:
    python change_folder_permissions.py grant    <folder> [--account NAME_EMAIL_OR_SID | --account-from PATH] [--take-ownership] [--dry-run]
    python change_folder_permissions.py lockdown <folder> [--account ... | --account-from PATH] [--exclude NAME_OR_PATH ...] [--dry-run]
    python change_folder_permissions.py restore  <changes.csv>

Examples:
    python change_folder_permissions.py grant "\\\\server\\share\\backup\\PC01"
    python change_folder_permissions.py grant D:\\data --account someone@example.org --dry-run
    python change_folder_permissions.py grant "\\\\server\\share\\data" --account-from "\\\\server\\share\\duplicates"
    python change_folder_permissions.py lockdown D:\\data --exclude logs --exclude "D:\\data\\private"
    python change_folder_permissions.py restore permissions_lockdown_20261016_101500_changes.csv

Permissions are read and written with the Windows security API (as icacls
does), so they're language-independent and every failure has its Windows error.
"""
import argparse
import csv
import ctypes
import os
import platform
import re
import subprocess
import sys
import time
from collections import Counter
from ctypes import wintypes
from datetime import datetime

# --------------------------------------------------------------------------
# Windows security API
# --------------------------------------------------------------------------

if platform.system() == "Windows":
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PVOID = ctypes.POINTER(ctypes.c_void_p)
    for _name, _args, _res in [
        ("GetNamedSecurityInfoW", [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, _PVOID, _PVOID, _PVOID, _PVOID, _PVOID], wintypes.DWORD),
        ("SetNamedSecurityInfoW", [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], wintypes.DWORD),
        ("ConvertSecurityDescriptorToStringSecurityDescriptorW", [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, _PVOID, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        ("ConvertStringSecurityDescriptorToSecurityDescriptorW", [wintypes.LPCWSTR, wintypes.DWORD, _PVOID, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        ("GetSecurityDescriptorDacl", [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), _PVOID, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
        ("ConvertStringSidToSidW", [wintypes.LPCWSTR, _PVOID], wintypes.BOOL),
        ("ConvertSidToStringSidW", [ctypes.c_void_p, _PVOID], wintypes.BOOL),
        ("LookupAccountNameW", [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int)], wintypes.BOOL),
        ("LookupAccountSidW", [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int)], wintypes.BOOL),
        ("OpenProcessToken", [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
        ("LookupPrivilegeValueW", [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p], wintypes.BOOL),
        ("AdjustTokenPrivileges", [wintypes.HANDLE, wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
    ]:
        _func = getattr(advapi32, _name)
        _func.argtypes, _func.restype = _args, _res
    kernel32.LocalFree.argtypes, kernel32.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x1
DACL_SECURITY_INFORMATION = 0x4
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
SDDL_REVISION_1 = 1
ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122

# Access masks, as in icacls: M = modify, RX = read & execute.
MODIFY = 0x1301BF
READ_EXECUTE = 0x1200A9
FILE_ALL, FILE_READ, FILE_WRITE, FILE_EXECUTE = 0x1F01FF, 0x120089, 0x120116, 0x1200A0
GENERIC_ALL, GENERIC_EXECUTE, GENERIC_WRITE, GENERIC_READ = 0x10000000, 0x20000000, 0x40000000, 0x80000000
SDDL_RIGHTS = {
    "GA": GENERIC_ALL, "GR": GENERIC_READ, "GW": GENERIC_WRITE, "GX": GENERIC_EXECUTE,
    "RC": 0x20000, "SD": 0x10000, "WD": 0x40000, "WO": 0x80000,
    "RP": 0x10, "WP": 0x20, "CC": 0x1, "DC": 0x2, "LC": 0x4, "SW": 0x8, "LO": 0x80, "DT": 0x40, "CR": 0x100,
    "FA": FILE_ALL, "FR": FILE_READ, "FW": FILE_WRITE, "FX": FILE_EXECUTE,
}
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"


class WinApiError(Exception):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"[WinError {code}] {ctypes.FormatError(code).strip()}")


def _check(ok) -> None:
    if not ok:
        raise WinApiError(ctypes.get_last_error())


def to_long_path(path: str) -> str:
    """\\\\?\\ form, so paths over 260 characters work."""
    if path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


def display_path(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    return path[4:] if path.startswith("\\\\?\\") else path


def read_security(path: str) -> tuple[str, str]:
    """(owner, DACL) of `path`, as SDDL strings."""
    sd = ctypes.c_void_p()
    info = OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    code = advapi32.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, info, None, None, None, None, ctypes.byref(sd))
    if code:
        raise WinApiError(code)
    try:
        text = ctypes.c_void_p()
        _check(advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, SDDL_REVISION_1, info, ctypes.byref(text), None))
        try:
            sddl = ctypes.wstring_at(text)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(sd)
    match = re.fullmatch(r"(?:O:(.*?))?(D:.*)", sddl)
    if not match:
        raise ValueError(f"unexpected security descriptor: {sddl}")
    return match.group(1) or "", match.group(2)


def write_dacl(path: str, dacl: "Dacl") -> None:
    """Set the explicit entries of `dacl`; Windows adds the inherited ones (unless protected)."""
    sd = ctypes.c_void_p()
    _check(advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(dacl.sddl(explicit_only=True), SDDL_REVISION_1, ctypes.byref(sd), None))
    try:
        present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        _check(advapi32.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)))
        flags = DACL_SECURITY_INFORMATION | (PROTECTED_DACL_SECURITY_INFORMATION if dacl.protected else UNPROTECTED_DACL_SECURITY_INFORMATION)
        code = advapi32.SetNamedSecurityInfoW(path, SE_FILE_OBJECT, flags, None, None, acl, None)
        if code:
            raise WinApiError(code)
    finally:
        kernel32.LocalFree(sd)


def write_owner(path: str, sid: str) -> None:
    psid = ctypes.c_void_p()
    _check(advapi32.ConvertStringSidToSidW(sid, ctypes.byref(psid)))
    try:
        code = advapi32.SetNamedSecurityInfoW(path, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION, psid, None, None, None)
        if code:
            raise WinApiError(code)
    finally:
        kernel32.LocalFree(psid)


_canonical: dict[str, str] = {}


def canonical_sid(sid: str) -> str:
    """S-1-... form of a SID string, also for SDDL aliases such as SY or BA."""
    if sid not in _canonical:
        psid, text = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            _check(advapi32.ConvertStringSidToSidW(sid, ctypes.byref(psid)))
            _check(advapi32.ConvertSidToStringSidW(psid, ctypes.byref(text)))
            _canonical[sid] = ctypes.wstring_at(text)
        except WinApiError:
            _canonical[sid] = sid
        finally:
            kernel32.LocalFree(psid)
            kernel32.LocalFree(text)
    return _canonical[sid]


def lookup_name(name: str) -> str | None:
    """SID of an account name (DOMAIN\\user, user, or user@domain), or None."""
    sid_size, domain_size, use = wintypes.DWORD(0), wintypes.DWORD(0), ctypes.c_int()
    advapi32.LookupAccountNameW(None, name, None, ctypes.byref(sid_size), None, ctypes.byref(domain_size), ctypes.byref(use))
    if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
        return None
    sid = ctypes.create_string_buffer(sid_size.value)
    domain = ctypes.create_unicode_buffer(domain_size.value)
    if not advapi32.LookupAccountNameW(None, name, sid, ctypes.byref(sid_size), domain, ctypes.byref(domain_size), ctypes.byref(use)):
        return None
    text = ctypes.c_void_p()
    _check(advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)))
    try:
        return ctypes.wstring_at(text)
    finally:
        kernel32.LocalFree(text)


def lookup_sid(sid: str) -> str:
    """DOMAIN\\name of a SID, or the SID itself when it can't be resolved."""
    psid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
        return sid
    try:
        name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        name_size, domain_size, use = wintypes.DWORD(256), wintypes.DWORD(256), ctypes.c_int()
        if not advapi32.LookupAccountSidW(None, psid, name, ctypes.byref(name_size), domain, ctypes.byref(domain_size), ctypes.byref(use)):
            return sid
        return f"{domain.value}\\{name.value}" if domain.value else name.value
    finally:
        kernel32.LocalFree(psid)


def lookup_email(email: str) -> str | None:
    """SID of the Active Directory user with this email address, or None."""
    escaped = re.sub(r"[\\*()\x00]", lambda m: f"\\{ord(m.group()):02x}", email).replace("'", "''")
    ldap = f"(&(objectCategory=person)(|(mail={escaped})(userPrincipalName={escaped})(proxyAddresses=smtp:{escaped})))"
    command = (
        f"$r = ([adsisearcher]'{ldap}').FindOne(); "
        "if ($r) { (New-Object System.Security.Principal.SecurityIdentifier($r.Properties['objectsid'][0], 0)).Value }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    sid = out.stdout.strip()
    return sid if sid.startswith("S-1-") else None


def resolve_account(account: str | None, account_from: str | None = None, folder: str = "") -> tuple[str, str]:
    """
    (SID, DOMAIN\\name) of --account (a name, email address or SID), of the
    owner of --account-from, or by default of the account running this.
    """
    if account_from:
        try:
            owner, _ = read_security(to_long_path(account_from))
        except WinApiError as e:
            raise SystemExit(f"ERROR: can't read the owner of '{account_from}': {e}")
        sid = canonical_sid(owner)
    elif account and account.upper().startswith("S-1-"):
        sid = canonical_sid(account)
    else:
        domain, user = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
        name = account or (f"{domain}\\{user}" if domain else user)
        sid = lookup_name(name)
        if sid is None and "@" in name:
            sid = lookup_email(name)
        if sid is None:
            raise SystemExit(f"ERROR: account '{name}' not found. Give it as DOMAIN\\user, user@domain, email address or SID.")
    if sid.startswith("S-1-12-") and display_path(folder).startswith("\\\\"):
        print(
            f"WARNING: {sid} is a cloud (Entra ID) account ID, as this PC knows the account. A file server in an\n"
            "on-premises domain usually knows it by another ID (S-1-5-21-...), and a grant to this one would not\n"
            "apply there. To use the server's ID: --account-from <a folder this account created on that share>,\n"
            "e.g. the duplicates folder made by quarantine_duplicates.py execute."
        )
    return sid, lookup_sid(sid)


def enable_privilege(name: str) -> bool:
    """Enable a privilege the account holds (e.g. SeTakeOwnershipPrivilege for an admin)."""

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Luid", LUID), ("Attributes", wintypes.DWORD)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x20 | 0x8, ctypes.byref(token)):  # ADJUST | QUERY
        return False
    try:
        privileges = TOKEN_PRIVILEGES(1, LUID(), 0x2)  # SE_PRIVILEGE_ENABLED
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(privileges.Luid)):
            return False
        advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None)
        return ctypes.get_last_error() == 0
    finally:
        kernel32.CloseHandle(token)


# --------------------------------------------------------------------------
# DACLs (as SDDL)
# --------------------------------------------------------------------------

def rights_mask(text: str) -> int:
    if text.lower().startswith("0x"):
        return int(text, 16)
    pairs = [text[i:i + 2] for i in range(0, len(text), 2)]
    if any(p not in SDDL_RIGHTS for p in pairs):
        raise ValueError(f"unknown access rights '{text}'")
    mask = 0
    for p in pairs:
        mask |= SDDL_RIGHTS[p]
    return mask


def file_rights(mask: int) -> int:
    """The mask with generic rights expanded into file rights."""
    for generic, specific in ((GENERIC_ALL, FILE_ALL), (GENERIC_READ, FILE_READ),
                              (GENERIC_WRITE, FILE_WRITE), (GENERIC_EXECUTE, FILE_EXECUTE)):
        if mask & generic:
            mask |= specific
    return mask & 0x0FFFFFFF


class Ace:
    """One entry: type (A allow, D deny, ...), flags (OI, CI, IO, ID, ...), rights, SID."""

    def __init__(self, text: str):
        self.fields = text.split(";")
        if len(self.fields) < 6:
            raise ValueError(f"unexpected entry ({text})")

    type = property(lambda self: self.fields[0])
    sid = property(lambda self: canonical_sid(self.fields[5]))
    flags = property(lambda self: {self.fields[1][i:i + 2] for i in range(0, len(self.fields[1]), 2)})
    inherited = property(lambda self: "ID" in self.flags)
    rights = property(lambda self: file_rights(rights_mask(self.fields[2])))

    @classmethod
    def allow(cls, sid: str, rights: int, inheritable: bool) -> "Ace":
        return cls(f"A;{'OICI' if inheritable else ''};0x{rights:x};;;{sid}")

    def with_rights(self, rights: int) -> "Ace":
        return Ace(";".join([self.fields[0], self.fields[1], f"0x{rights:x}", *self.fields[3:]]))

    def as_explicit(self) -> "Ace":
        flags = "".join(sorted(f for f in self.flags if f != "ID"))
        return Ace(";".join([self.fields[0], flags, *self.fields[2:]]))

    def sddl(self) -> str:
        return f"({';'.join(self.fields)})"


class Dacl:
    def __init__(self, sddl: str):
        match = re.fullmatch(r"D:([A-Z_]*)((?:\([^()]*\))*)", sddl)
        if not match:
            raise ValueError(f"unsupported permissions (conditional entries?): {sddl}")
        self.null = "NO_ACCESS_CONTROL" in match.group(1)
        self.protected = "P" in match.group(1).replace("NO_ACCESS_CONTROL", "")
        self.aces = [Ace(a) for a in re.findall(r"\(([^()]*)\)", match.group(2))]

    def sddl(self, explicit_only: bool = False) -> str:
        aces = [a for a in self.aces if not (explicit_only and a.inherited)]
        return "D:" + ("P" if self.protected else "") + "".join(a.sddl() for a in aces)

    def grants(self, sid: str, rights: int, inheritable: bool = False) -> bool:
        """Does an allow entry give `sid` all of `rights` on this item (and its children)?"""
        if self.null:
            return True
        return any(
            a.type == "A" and a.sid == sid and "IO" not in a.flags and a.rights & rights == rights
            and (not inheritable or {"OI", "CI"} <= a.flags)
            for a in self.aces
        )

    def add_explicit(self, ace: Ace) -> None:
        """Add after the explicit entries (canonical order: explicit before inherited)."""
        position = next((i for i, a in enumerate(self.aces) if a.inherited), len(self.aces))
        self.aces.insert(position, ace)

    def reduce_to_read(self, keep: set[str], include_inherited: bool) -> Counter:
        """Allow entries of everyone not in `keep` keep only their read rights. Returns SID -> entries changed."""
        reduced: Counter = Counter()
        aces = []
        for a in self.aces:
            if include_inherited and a.inherited:
                a = a.as_explicit()
            if a.type == "A" and not a.inherited and a.sid not in keep and a.rights & ~READ_EXECUTE:
                reduced[a.sid] += 1
                read = a.rights & READ_EXECUTE
                if not read:
                    continue  # a write-only entry: dropped rather than turned into new read access
                a = a.with_rights(read)
            aces.append(a)
        self.aces = aces
        return reduced


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

CHANGE_COLUMNS = ["path", "type", "owner_before", "owner_after", "dacl_before", "dacl_after"]
ERROR_COLUMNS = ["timestamp", "step", "path", "operation", "error_code", "message"]


class Run:
    """Output files, counters and progress for one step."""

    def __init__(self, step: str, output: str, dry_run: bool):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(output, f"permissions_{step}_{stamp}{'_dryrun' if dry_run else ''}")
        os.makedirs(output, exist_ok=True)
        number = 1
        while os.path.exists(f"{base}_changes.csv"):  # two runs within the same second
            number += 1
            base = os.path.join(output, f"permissions_{step}_{stamp}-{number}{'_dryrun' if dry_run else ''}")
        self.step, self.dry_run = step, dry_run
        self.changes_path, self.errors_path = f"{base}_changes.csv", f"{base}_errors.csv"
        self._changes_file = open(self.changes_path, "w", newline="", encoding="utf-8")
        self._errors_file = open(self.errors_path, "w", newline="", encoding="utf-8")
        self.changes, self.errors = csv.writer(self._changes_file), csv.writer(self._errors_file)
        self.changes.writerow(CHANGE_COLUMNS)
        self.errors.writerow(ERROR_COLUMNS)
        self.counts: Counter = Counter()
        self.error_messages: Counter = Counter()
        self._last_print = time.monotonic()
        self._progress_shown = False
        self._last_path, self._last_count = "", 0

    def say(self, message: str) -> None:
        if self._progress_shown:
            print()
            self._progress_shown = False
        print(message)

    def change(self, path, is_dir, owner_before, owner_after, dacl_before, dacl_after) -> None:
        # Written before the change is made, so a killed run can still be restored.
        self.changes.writerow([display_path(path), "dir" if is_dir else "file", owner_before, owner_after, dacl_before, dacl_after])
        self._changes_file.flush()
        self.counts["changed"] += 1
        if path != self._last_path:
            self._last_path, self._last_count = path, 0
        self._last_count += 1

    def error(self, path: str, operation: str, error: Exception) -> None:
        if path == self._last_path:  # its recorded change(s) didn't happen
            self.counts["changed"] -= self._last_count
            self._last_count = 0
        code = getattr(error, "code", None) or getattr(error, "winerror", None) or ""
        message = str(error)
        self.errors.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), self.step, display_path(path), operation, code, message])
        self._errors_file.flush()
        self.counts["errors"] += 1
        self.error_messages[f"{operation}: {message}"] += 1

    def checked(self) -> None:
        self.counts["checked"] += 1
        if time.monotonic() - self._last_print > 2:
            self._last_print = time.monotonic()
            self._progress_shown = True
            print(f"\r  {self.counts['checked']:,} checked | {self.counts['changed']:,} changed | {self.counts['errors']:,} errors",
                  end="", flush=True)

    def close(self) -> None:
        print(f"\r  {self.counts['checked']:,} checked | {self.counts['changed']:,} changed | {self.counts['errors']:,} errors")
        self._progress_shown = False
        self._changes_file.close()
        self._errors_file.close()
        for message, count in self.error_messages.most_common(10):
            print(f"  {count:,} x {message}")
        print(f"Changes{' (dry run, nothing changed)' if self.dry_run else ''}: {self.changes_path}")
        print(f"Errors: {self.errors_path}")


def walk(root: str, run: Run, skip=lambda path, name: False):
    """(path, is_dir) of everything under `root`, parents before their contents."""
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: run.error(e.filename or root, "list folder", e)):
        dirnames[:] = [d for d in dirnames if not skip(os.path.join(dirpath, d), d)]
        for name in dirnames:
            yield os.path.join(dirpath, name), True
        for name in filenames:
            yield os.path.join(dirpath, name), False


def check_folder(folder: str, output: str) -> str:
    if not os.path.isdir(to_long_path(folder)):
        raise SystemExit(f"ERROR: '{folder}' is not an existing folder.")
    folder_key = os.path.normcase(os.path.abspath(folder)).rstrip("\\") + "\\"
    if (os.path.normcase(os.path.abspath(output)).rstrip("\\") + "\\").startswith(folder_key):
        raise SystemExit(f"ERROR: --output '{output}' is inside '{folder}'; write the logs somewhere else.")
    return to_long_path(folder)


def confirm(message: str, args) -> None:
    print(message)
    if args.dry_run or args.yes:
        return
    if input("Type 'yes' to continue: ").strip().lower() != "yes":
        raise SystemExit("Cancelled; nothing was changed.")


def cmd_grant(args) -> None:
    root = check_folder(args.folder, args.output)
    sid, name = resolve_account(args.account, args.account_from, root)
    confirm(f"Grant {name} ({sid}) Modify on '{display_path(root)}' and everything under it.", args)
    me = None  # new owner for --take-ownership: the account, with the same ID as the grant
    if args.take_ownership:
        me = sid
        enable_privilege("SeTakeOwnershipPrivilege")
        enable_privilege("SeRestorePrivilege")
    run = Run("grant", args.output, args.dry_run)
    # Dry run: nothing is changed, so whether an item will inherit the new
    # entry is worked out from its parent (folder -> its children inherit it).
    inheritable: dict[str, bool] = {}

    def _take_ownership(path: str, is_dir: bool, owner: str, dacl: str) -> None:
        # Recorded first: the owner from before (if it could be read) is what restore puts back.
        run.change(path, is_dir, owner, me, dacl, dacl)
        if not args.dry_run:
            write_owner(path, me)

    def _grant(path: str, is_dir: bool, parent_ok: bool) -> bool:
        """Grant on one item; returns whether its children inherit the grant."""
        run.checked()
        operation = "read permissions"
        try:
            try:
                owner, sddl = read_security(path)
            except WinApiError as e:
                if not (me and e.code == ERROR_ACCESS_DENIED) or args.dry_run:
                    raise
                operation = "take ownership"
                _take_ownership(path, is_dir, "", "")  # owner unknown: it couldn't be read
                operation = "read permissions"
                owner, sddl = read_security(path)
            dacl = Dacl(sddl)
            will_inherit = args.dry_run and parent_ok and not dacl.protected
            if dacl.grants(sid, MODIFY) or will_inherit:
                return dacl.grants(sid, MODIFY, inheritable=True) or will_inherit
            operation = "grant"
            before = dacl.sddl()
            dacl.add_explicit(Ace.allow(sid, MODIFY, inheritable=is_dir))
            run.change(path, is_dir, owner, owner, before, dacl.sddl())
            if not args.dry_run:
                try:
                    write_dacl(path, dacl)
                except WinApiError as e:
                    if not (me and e.code == ERROR_ACCESS_DENIED):
                        raise
                    operation = "take ownership"
                    _take_ownership(path, is_dir, owner, before)
                    operation = "grant"
                    write_dacl(path, dacl)
            return is_dir
        except (WinApiError, ValueError, OSError) as e:
            run.error(path, operation, e)
            return False

    run.say("Root folder (Windows then passes the entry on to everything that inherits; on a large tree this takes a while) ...")
    inheritable[root] = _grant(root, True, False)
    run.say("Checking everything under it ...")
    try:
        for path, is_dir in walk(root, run):
            ok = _grant(path, is_dir, inheritable.get(os.path.dirname(path), False))
            if is_dir:
                inheritable[path] = ok
    finally:
        run.close()
    if not args.dry_run:
        print(f"Done. Items in the errors file still lack the grant{'' if me else '; --take-ownership may fix the Access denied ones'}.")


def cmd_lockdown(args) -> None:
    root = check_folder(args.folder, args.output)
    sid, name = resolve_account(args.account, args.account_from, root)
    keep ={sid, SYSTEM_SID, ADMINISTRATORS_SID}
    excludes = {os.path.normcase(os.path.normpath(e)) for e in args.exclude}
    excluded: list[str] = []

    def _skip(path: str, dir_name: str) -> bool:
        if os.path.normcase(dir_name) in excludes or os.path.normcase(display_path(path)) in excludes:
            excluded.append(path)
            return True
        return False

    confirm(
        f"Lock down '{display_path(root)}': every user/group with write access gets read only.\n"
        f"Kept as they are: {name}, SYSTEM, Administrators"
        + (f"; excluded folders: {', '.join(args.exclude)}" if args.exclude else "") + ".",
        args,
    )
    run = Run("lockdown", args.output, args.dry_run)
    reduced: Counter = Counter()

    def _apply(path: str, is_dir: bool, change) -> None:
        run.checked()
        try:
            owner, sddl = read_security(path)
            dacl = Dacl(sddl)
            if dacl.null:
                raise ValueError("no permissions set (everyone has full access); not changed, set them by hand")
            before = dacl.sddl()
            if change(dacl):
                run.change(path, is_dir, owner, owner, before, dacl.sddl())
                if not args.dry_run:
                    write_dacl(path, dacl)
        except (WinApiError, ValueError, OSError) as e:
            run.error(path, "lockdown", e)

    def _protect(dacl: Dacl) -> bool:
        # Excluded folder: stop inheriting from the root, keeping what it has now.
        if dacl.protected:
            return False
        dacl.aces = [a.as_explicit() for a in dacl.aces]
        dacl.protected = True
        return True

    def _reduce(dacl: Dacl) -> bool:
        # Inherited entries follow the root; only this item's own entries are changed.
        counts = dacl.reduce_to_read(keep, include_inherited=False)
        reduced.update(counts)
        return bool(counts)

    def _reduce_root(dacl: Dacl) -> bool:
        # The root stops inheriting from above, so groups given write access
        # higher up are reduced too; the account keeps (or gets) Modify.
        changed = not dacl.protected
        dacl.protected = True
        counts = dacl.reduce_to_read(keep, include_inherited=True)
        reduced.update(counts)
        if not dacl.grants(sid, MODIFY, inheritable=True):
            dacl.add_explicit(Ace.allow(sid, MODIFY, inheritable=True))
            changed = True
        return changed or bool(counts)

    run.say("Checking everything under the root ...")
    try:
        # Contents first, the root last: excluded folders must be detached
        # before the root's reduced entries are passed on to them.
        for path, is_dir in walk(root, run, skip=_skip):
            _apply(path, is_dir, _reduce)
        for path in excluded:
            _apply(path, True, _protect)
        run.say("Root folder (Windows then passes the change on to everything that inherits; on a large tree this takes a while) ...")
        _apply(root, True, _reduce_root)
    finally:
        run.close()
    for reduced_sid, count in reduced.most_common():
        print(f"  reduced to read: {lookup_sid(reduced_sid)} ({count:,} entries)")


def cmd_restore(args) -> None:
    if "_dryrun_" in os.path.basename(args.changes_csv):
        raise SystemExit("ERROR: that's a dry-run file; nothing was changed in that run.")
    with open(args.changes_csv, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or any(c not in rows[0] for c in CHANGE_COLUMNS):
        raise SystemExit(f"ERROR: '{args.changes_csv}' is not a changes file written by this tool.")
    confirm(f"Restore the permissions of {len(rows):,} item(s) from '{args.changes_csv}'.", args)
    if any(r["owner_before"] and r["owner_before"] != r["owner_after"] for r in rows):
        enable_privilege("SeRestorePrivilege")
    run = Run("restore", args.output, args.dry_run)
    try:
        # Same order as they were changed: for a lockdown, the root last.
        for row in rows:
            path = to_long_path(row["path"])
            run.checked()
            try:
                owner, sddl = read_security(path)
                dacl_now, dacl_before = Dacl(sddl), Dacl(row["dacl_before"]) if row["dacl_before"] else None
                set_dacl = dacl_before is not None and dacl_now.sddl(explicit_only=True) != dacl_before.sddl(explicit_only=True)
                set_owner = row["owner_before"] and canonical_sid(row["owner_before"]) != canonical_sid(owner)
                if not (set_dacl or set_owner):
                    continue  # already as before (e.g. its change had failed)
                run.change(path, row["type"] == "dir", owner, row["owner_before"] or owner, sddl, row["dacl_before"] or sddl)
                if args.dry_run:
                    continue
                if set_dacl:
                    write_dacl(path, dacl_before)
                if set_owner:
                    write_owner(path, row["owner_before"])
            except (WinApiError, ValueError, OSError) as e:
                run.error(path, "restore", e)
    finally:
        run.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Grant Modify on a folder tree, lock it down to read-only, or restore it (Windows).")
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(p, account: bool = True):
        if account:
            who = p.add_mutually_exclusive_group()
            who.add_argument("--account", help="Account to grant/keep: DOMAIN\\user, user@domain, email address or SID. Default: you.")
            who.add_argument("--account-from", metavar="PATH",
                             help="Use the owner of PATH as the account (e.g. a folder you created on the share: how the server knows you).")
        p.add_argument("--output", default=".", help="Folder for the changes and errors files. Default: the current folder.")
        p.add_argument("--dry-run", action="store_true", help="Write the changes file, but change nothing.")
        p.add_argument("--yes", action="store_true", help="Don't ask for confirmation.")

    grant = sub.add_parser("grant", help="Give the account Modify on the folder and everything under it.")
    grant.add_argument("folder")
    grant.add_argument("--take-ownership", action="store_true",
                       help="For items whose permissions can't be read or changed (Access denied): take ownership, then grant. The original owner is recorded.")
    _common(grant)

    lockdown = sub.add_parser("lockdown", help="Reduce every other user/group with write access to read.")
    lockdown.add_argument("folder")
    lockdown.add_argument("--exclude", action="append", default=[], help="Folder name or full path to leave as it is (can be repeated).")
    _common(lockdown)

    restore = sub.add_parser("restore", help="Put back the permissions from before a grant or lockdown run.")
    restore.add_argument("changes_csv", help="The permissions_<step>_<timestamp>_changes.csv of that run.")
    _common(restore, account=False)

    args = parser.parse_args()
    if platform.system() != "Windows":
        raise SystemExit("ERROR: this tool requires Windows.")
    {"grant": cmd_grant, "lockdown": cmd_lockdown, "restore": cmd_restore}[args.command](args)


if __name__ == "__main__":
    main()
