# SPDX-License-Identifier: Apache-2.0
# Derived from Firmware-CLI commit 0e8a45d; modified for BYO-Firmware-MCP.
"""Fail-closed private-file primitives for formal CAN evidence.

The public CAN protocol is intentionally independent of this module.  This is
an internal portability seam which keeps the existing POSIX durability
contract and uses Win32 handles/ACLs on Windows, where POSIX mode bits are not
an access-control proof.

Windows support is deliberately narrow: paths must live on a local NTFS
volume, the caller-owned parent must not grant write-like access to an
untrusted principal, and every CAN-created object has a protected DACL granting
full access only to the current user and LOCAL_SYSTEM.  Any API or validation
failure is an operation failure; there is no permissive fallback.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _is_windows() -> bool:
    """Platform predicate kept injectable for fail-closed seam tests."""

    return os.name == "nt"


def _posix_owner_is_current(metadata: os.stat_result) -> bool:
    getuid = getattr(os, "getuid", None)
    return not callable(getuid) or metadata.st_uid == getuid()


def _stable_regular_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _fsync_directory_posix(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _posix_directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:  # pragma: no cover - POSIX portability guard
        raise OSError("secure POSIX CAN paths require O_NOFOLLOW and O_DIRECTORY")
    return os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)


def _open_directory_chain_posix(
    path: Path,
    *,
    create_parents: bool = False,
    create_final: bool = False,
    private_final: bool = False,
) -> int:
    """Open an absolute directory one component at a time without following links."""

    absolute = Path(os.path.abspath(path))
    parts = absolute.parts
    if not parts:
        raise OSError(f"CAN directory has no filesystem root: {absolute}")
    if len(parts) == 1 and (create_parents or create_final or private_final):
        raise OSError(f"CAN private directory cannot be a filesystem root: {absolute}")
    flags = _posix_directory_flags()
    current = os.open(absolute.anchor, flags)
    try:
        for index, component in enumerate(parts[1:], start=1):
            final = index == len(parts) - 1
            following: int | None = None
            try:
                following = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not (create_parents or (create_final and final)):
                    raise
                os.mkdir(component, PRIVATE_DIRECTORY_MODE, dir_fd=current)
                os.fsync(current)
                following = os.open(component, flags, dir_fd=current)
            try:
                metadata = os.fstat(following)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise OSError(f"CAN path component is not a directory: {absolute}")
                if final and private_final:
                    os.fchmod(following, PRIVATE_DIRECTORY_MODE)
                    metadata = os.fstat(following)
                    if stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE:
                        raise OSError(f"CAN private directory does not have mode 0700: {absolute}")
                    if not _posix_owner_is_current(metadata):
                        raise OSError(f"CAN private directory has a different owner: {absolute}")
                    os.fsync(following)
                    os.fsync(current)
            except BaseException:
                os.close(following)
                raise
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _ensure_private_directory_posix(path: Path, *, parents: bool) -> None:
    descriptor = _open_directory_chain_posix(
        path,
        create_parents=parents,
        create_final=True,
        private_final=True,
    )
    os.close(descriptor)


@dataclass(frozen=True)
class _WindowsFileIdentity:
    volume_serial: int
    file_index: int
    links: int
    size: int
    write_time: int


class _WindowsApi:
    """Small, eagerly validated Win32 API surface.

    It is instantiated only on Windows.  Keeping all ``ctypes`` declarations
    behind that boundary lets Linux/macOS import and type-check the package
    without manufacturing Windows-only symbols.
    """

    # File access/sharing/creation.
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    READ_CONTROL = 0x00020000
    FILE_READ_ATTRIBUTES = 0x00000080
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    CREATE_NEW = 1
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_READONLY = 0x00000001
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_FLAG_WRITE_THROUGH = 0x80000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    MOVEFILE_REPLACE_EXISTING = 0x00000001
    MOVEFILE_WRITE_THROUGH = 0x00000008
    DRIVE_FIXED = 3
    ERROR_FILE_EXISTS = 80
    ERROR_ALREADY_EXISTS = 183

    # Security information/control.
    OWNER_SECURITY_INFORMATION = 0x00000001
    DACL_SECURITY_INFORMATION = 0x00000004
    SE_FILE_OBJECT = 1
    SE_DACL_PROTECTED = 0x1000
    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1
    SDDL_REVISION_1 = 1
    ACL_SIZE_INFORMATION_CLASS = 2
    ACCESS_ALLOWED_ACE_TYPE = 0
    _COMPLEX_ALLOWED_ACE_TYPES = frozenset({5, 9, 11})
    _INHERIT_ONLY_ACE = 0x08

    # Access bits whose grant to a principal other than current user, SYSTEM,
    # or Administrators makes a caller-owned root attacker-writable.
    _WRITE_LIKE_ACCESS = (
        0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
        | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
        | 0x00000010  # FILE_WRITE_EA
        | 0x00000040  # FILE_DELETE_CHILD
        | 0x00000100  # FILE_WRITE_ATTRIBUTES
        | 0x00010000  # DELETE
        | 0x00040000  # WRITE_DAC
        | 0x00080000  # WRITE_OWNER
        | 0x10000000  # GENERIC_ALL
        | 0x40000000  # GENERIC_WRITE
    )

    def __init__(self) -> None:  # pragma: no cover - exercised on native Windows CI
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        win_dll = getattr(ctypes, "WinDLL")
        self.kernel32 = win_dll("kernel32", use_last_error=True)
        self.advapi32 = win_dll("advapi32", use_last_error=True)
        self.invalid_handle = ctypes.c_void_p(-1).value

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL),
            ]

        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
            _fields_ = [
                ("FileAttributes", wintypes.DWORD),
                ("ReparseTag", wintypes.DWORD),
            ]

        class ACL_SIZE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("AceCount", wintypes.DWORD),
                ("AclBytesInUse", wintypes.DWORD),
                ("AclBytesFree", wintypes.DWORD),
            ]

        class ACE_HEADER(ctypes.Structure):
            _fields_ = [
                ("AceType", ctypes.c_ubyte),
                ("AceFlags", ctypes.c_ubyte),
                ("AceSize", wintypes.WORD),
            ]

        class TOKEN_USER(ctypes.Structure):
            _fields_ = [("User", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

        self.SECURITY_ATTRIBUTES = SECURITY_ATTRIBUTES
        self.BY_HANDLE_FILE_INFORMATION = BY_HANDLE_FILE_INFORMATION
        self.FILE_ATTRIBUTE_TAG_INFO = FILE_ATTRIBUTE_TAG_INFO
        self.ACL_SIZE_INFORMATION_STRUCT = ACL_SIZE_INFORMATION
        self.ACE_HEADER = ACE_HEADER
        self.TOKEN_USER_STRUCT = TOKEN_USER

        k32 = self.kernel32
        adv = self.advapi32
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(SECURITY_ATTRIBUTES),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateDirectoryW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(SECURITY_ATTRIBUTES),
        ]
        k32.CreateDirectoryW.restype = wintypes.BOOL
        k32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
        ]
        k32.GetFileInformationByHandle.restype = wintypes.BOOL
        k32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        k32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        k32.WriteFile.restype = wintypes.BOOL
        k32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        k32.ReadFile.restype = wintypes.BOOL
        k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        k32.FlushFileBuffers.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.DeleteFileW.argtypes = [wintypes.LPCWSTR]
        k32.DeleteFileW.restype = wintypes.BOOL
        k32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        k32.MoveFileExW.restype = wintypes.BOOL
        k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.GetVolumePathNameW.restype = wintypes.BOOL
        k32.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        k32.GetVolumeInformationW.restype = wintypes.BOOL
        k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        k32.GetDriveTypeW.restype = wintypes.UINT
        k32.GetCurrentProcess.argtypes = []
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.LocalFree.argtypes = [wintypes.HLOCAL]
        k32.LocalFree.restype = wintypes.HLOCAL

        adv.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        adv.GetTokenInformation.restype = wintypes.BOOL
        adv.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
        adv.ConvertSidToStringSidW.restype = wintypes.BOOL
        adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)]
        adv.ConvertStringSidToSidW.restype = wintypes.BOOL
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.ULONG),
        ]
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        adv.GetSecurityInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
        ]
        adv.GetSecurityInfo.restype = wintypes.DWORD
        adv.GetSecurityDescriptorDacl.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        ]
        adv.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        adv.GetSecurityDescriptorOwner.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        ]
        adv.GetSecurityDescriptorOwner.restype = wintypes.BOOL
        adv.GetSecurityDescriptorControl.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        adv.GetSecurityDescriptorControl.restype = wintypes.BOOL
        adv.GetAclInformation.argtypes = [
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.c_int,
        ]
        adv.GetAclInformation.restype = wintypes.BOOL
        adv.GetAce.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
        ]
        adv.GetAce.restype = wintypes.BOOL
        adv.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
        adv.EqualSid.restype = wintypes.BOOL

        self._token_buffer: Any | None = None
        self._system_sid: Any | None = None
        self._administrators_sid: Any | None = None
        self._owner_rights_sid: Any | None = None
        self._initialize_security()

    def _raise_last_error(self, operation: str) -> None:  # pragma: no cover - Windows only
        win_error = getattr(self.ctypes, "WinError")
        get_last_error = getattr(self.ctypes, "get_last_error")
        raise win_error(get_last_error(), operation)

    def _initialize_security(self) -> None:  # pragma: no cover - Windows only
        c = self.ctypes
        w = self.wintypes
        token = w.HANDLE()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(), self.TOKEN_QUERY, c.byref(token)
        ):
            self._raise_last_error("OpenProcessToken")
        try:
            needed = w.DWORD()
            self.advapi32.GetTokenInformation(token, self.TOKEN_USER, None, 0, c.byref(needed))
            if needed.value == 0:
                self._raise_last_error("GetTokenInformation(size)")
            self._token_buffer = c.create_string_buffer(needed.value)
            if not self.advapi32.GetTokenInformation(
                token,
                self.TOKEN_USER,
                self._token_buffer,
                needed,
                c.byref(needed),
            ):
                self._raise_last_error("GetTokenInformation")
            token_user = c.cast(self._token_buffer, c.POINTER(self.TOKEN_USER_STRUCT)).contents
            self.current_user_sid = token_user.User
            sid_string = w.LPWSTR()
            if not self.advapi32.ConvertSidToStringSidW(self.current_user_sid, c.byref(sid_string)):
                self._raise_last_error("ConvertSidToStringSidW")
            try:
                current_sid_text = sid_string.value
            finally:
                self.kernel32.LocalFree(sid_string)
        finally:
            self.kernel32.CloseHandle(token)

        sddl = f"O:{current_sid_text}D:P(A;;FA;;;SY)(A;;FA;;;{current_sid_text})"
        descriptor = w.LPVOID()
        descriptor_size = w.ULONG()
        if not self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            self.SDDL_REVISION_1,
            c.byref(descriptor),
            c.byref(descriptor_size),
        ):
            self._raise_last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        self._security_descriptor = descriptor
        self.security_attributes = self.SECURITY_ATTRIBUTES(
            c.sizeof(self.SECURITY_ATTRIBUTES), descriptor, False
        )
        self.expected_owner, self.expected_dacl = self._owner_and_dacl(descriptor)
        self._system_sid = self._sid_from_string("S-1-5-18")
        self._administrators_sid = self._sid_from_string("S-1-5-32-544")
        self._owner_rights_sid = self._sid_from_string("S-1-3-4")

    def _sid_from_string(self, value: str) -> Any:  # pragma: no cover - Windows only
        sid = self.wintypes.LPVOID()
        if not self.advapi32.ConvertStringSidToSidW(value, self.ctypes.byref(sid)):
            self._raise_last_error("ConvertStringSidToSidW")
        return sid

    def _owner_and_dacl(self, descriptor: Any) -> tuple[Any, Any]:  # pragma: no cover
        c = self.ctypes
        w = self.wintypes
        owner = w.LPVOID()
        owner_defaulted = w.BOOL()
        if not self.advapi32.GetSecurityDescriptorOwner(
            descriptor, c.byref(owner), c.byref(owner_defaulted)
        ):
            self._raise_last_error("GetSecurityDescriptorOwner")
        present = w.BOOL()
        dacl = w.LPVOID()
        defaulted = w.BOOL()
        if not self.advapi32.GetSecurityDescriptorDacl(
            descriptor, c.byref(present), c.byref(dacl), c.byref(defaulted)
        ):
            self._raise_last_error("GetSecurityDescriptorDacl")
        if not present.value or not dacl:
            raise OSError("Windows CAN private object has no DACL")
        return owner, dacl

    def _acl_bytes(self, acl: Any) -> bytes:  # pragma: no cover - Windows only
        info = self.ACL_SIZE_INFORMATION_STRUCT()
        if not self.advapi32.GetAclInformation(
            acl,
            self.ctypes.byref(info),
            self.ctypes.sizeof(info),
            self.ACL_SIZE_INFORMATION_CLASS,
        ):
            self._raise_last_error("GetAclInformation")
        return self.ctypes.string_at(acl, info.AclBytesInUse)

    def _get_security(self, handle: int) -> tuple[Any, Any, Any]:  # pragma: no cover
        c = self.ctypes
        w = self.wintypes
        owner = w.LPVOID()
        dacl = w.LPVOID()
        descriptor = w.LPVOID()
        status = self.advapi32.GetSecurityInfo(
            handle,
            self.SE_FILE_OBJECT,
            self.OWNER_SECURITY_INFORMATION | self.DACL_SECURITY_INFORMATION,
            c.byref(owner),
            None,
            c.byref(dacl),
            None,
            c.byref(descriptor),
        )
        if status:
            raise OSError(status, "GetSecurityInfo")
        if not owner or not dacl or not descriptor:
            if descriptor:
                self.kernel32.LocalFree(descriptor)
            raise OSError("Windows CAN private object has incomplete security metadata")
        return owner, dacl, descriptor

    def _sid_to_string(self, sid: Any) -> str:  # pragma: no cover - Windows only
        sid_string = self.wintypes.LPWSTR()
        if not self.advapi32.ConvertSidToStringSidW(sid, self.ctypes.byref(sid_string)):
            self._raise_last_error("ConvertSidToStringSidW")
        try:
            return str(sid_string.value)
        finally:
            self.kernel32.LocalFree(sid_string)

    def _untrusted_writer(self, dacl: Any) -> tuple[str, int] | None:  # pragma: no cover
        c = self.ctypes
        info = self.ACL_SIZE_INFORMATION_STRUCT()
        if not self.advapi32.GetAclInformation(
            dacl, c.byref(info), c.sizeof(info), self.ACL_SIZE_INFORMATION_CLASS
        ):
            self._raise_last_error("GetAclInformation")
        # OWNER RIGHTS (S-1-3-4) is a Windows security principal whose access
        # applies to the object's owner, not to an unrelated account.  It is
        # safe here only because `_validate_security` first proves that the
        # owner belongs to the same finite trusted set used below.  Python's
        # Windows temporary-directory setup can preserve this ACE on a child
        # even when its parent has a minimal protected DACL.
        trusted = (
            self.current_user_sid,
            self._system_sid,
            self._administrators_sid,
            self._owner_rights_sid,
        )
        for index in range(info.AceCount):
            ace = self.wintypes.LPVOID()
            if not self.advapi32.GetAce(dacl, index, c.byref(ace)):
                self._raise_last_error("GetAce")
            header = c.cast(ace, c.POINTER(self.ACE_HEADER)).contents
            if header.AceType in self._COMPLEX_ALLOWED_ACE_TYPES:
                raise OSError("Windows CAN root contains an unsupported allow ACE")
            if header.AceType != self.ACCESS_ALLOWED_ACE_TYPE:
                continue
            if header.AceFlags & self._INHERIT_ONLY_ACE:
                continue
            address_value = c.cast(ace, c.c_void_p).value
            if address_value is None:
                raise OSError("Windows CAN DACL contains a null ACE")
            address = int(address_value)
            mask = c.c_uint32.from_address(address + c.sizeof(self.ACE_HEADER)).value
            sid = c.c_void_p(address + c.sizeof(self.ACE_HEADER) + c.sizeof(c.c_uint32))
            if mask & self._WRITE_LIKE_ACCESS and not any(
                self.advapi32.EqualSid(sid, candidate) for candidate in trusted
            ):
                return self._sid_to_string(sid), mask
        return None

    def _file_information(
        self, handle: int
    ) -> tuple[_WindowsFileIdentity, int]:  # pragma: no cover
        info = self.BY_HANDLE_FILE_INFORMATION()
        if not self.kernel32.GetFileInformationByHandle(handle, self.ctypes.byref(info)):
            self._raise_last_error("GetFileInformationByHandle")
        tag = self.FILE_ATTRIBUTE_TAG_INFO()
        # FILE_INFO_BY_HANDLE_CLASS.FileAttributeTagInfo == 9.
        if not self.kernel32.GetFileInformationByHandleEx(
            handle, 9, self.ctypes.byref(tag), self.ctypes.sizeof(tag)
        ):
            self._raise_last_error("GetFileInformationByHandleEx(FileAttributeTagInfo)")
        if tag.FileAttributes & self.FILE_ATTRIBUTE_REPARSE_POINT or tag.ReparseTag:
            raise OSError("Windows CAN path is a reparse point")
        identity = _WindowsFileIdentity(
            volume_serial=int(info.dwVolumeSerialNumber),
            file_index=(int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow),
            links=int(info.nNumberOfLinks),
            size=(int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow),
            write_time=(int(info.ftLastWriteTime.dwHighDateTime) << 32)
            | int(info.ftLastWriteTime.dwLowDateTime),
        )
        return identity, int(tag.FileAttributes)

    def _validate_security(self, handle: int, *, exact: bool) -> None:  # pragma: no cover
        owner, dacl, descriptor = self._get_security(handle)
        try:
            owner_is_current_user = bool(self.advapi32.EqualSid(owner, self.current_user_sid))
            if exact:
                if not owner_is_current_user:
                    raise OSError("Windows CAN private object has a different owner")
                control = self.wintypes.WORD()
                revision = self.wintypes.DWORD()
                if not self.advapi32.GetSecurityDescriptorControl(
                    descriptor, self.ctypes.byref(control), self.ctypes.byref(revision)
                ):
                    self._raise_last_error("GetSecurityDescriptorControl")
                if not control.value & self.SE_DACL_PROTECTED:
                    raise OSError("Windows CAN private object DACL is not protected")
                if not hmac.compare_digest(
                    self._acl_bytes(dacl), self._acl_bytes(self.expected_dacl)
                ):
                    raise OSError("Windows CAN private object DACL is not the expected DACL")
            else:
                # An administrator account can create inherited temporary/run
                # roots whose Windows owner is the Administrators group rather
                # than the token's user SID. SYSTEM and Administrators are
                # already the only non-user principals trusted by the root
                # DACL check, so accept the same finite owner set here. Files
                # and directories created by this seam still require the exact
                # current-user owner and protected DACL above.
                trusted_root_owner = owner_is_current_user or any(
                    self.advapi32.EqualSid(owner, candidate)
                    for candidate in (self._system_sid, self._administrators_sid)
                )
                if not trusted_root_owner:
                    raise OSError("Windows CAN root has an untrusted owner")
                untrusted_writer = self._untrusted_writer(dacl)
                if untrusted_writer is not None:
                    sid, mask = untrusted_writer
                    raise OSError(
                        "Windows CAN root grants write access to an untrusted principal "
                        f"({sid}, mask=0x{mask:08x})"
                    )
        finally:
            self.kernel32.LocalFree(descriptor)

    def validate_volume(self, path: Path) -> Path:  # pragma: no cover - Windows only
        c = self.ctypes
        absolute = Path(os.path.abspath(path))
        probe = absolute
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        volume = c.create_unicode_buffer(32768)
        if not self.kernel32.GetVolumePathNameW(str(probe), volume, len(volume)):
            self._raise_last_error("GetVolumePathNameW")
        if self.kernel32.GetDriveTypeW(volume.value) != self.DRIVE_FIXED:
            raise OSError("Windows CAN run root must be on a local fixed drive")
        filesystem = c.create_unicode_buffer(64)
        if not self.kernel32.GetVolumeInformationW(
            volume.value, None, 0, None, None, None, filesystem, len(filesystem)
        ):
            self._raise_last_error("GetVolumeInformationW")
        if filesystem.value.upper() != "NTFS":
            raise OSError("Windows CAN run root must be on NTFS")
        return Path(volume.value)

    def open_existing(
        self, path: Path, *, directory: bool, read: bool = False, lock_rename: bool = False
    ) -> int:
        # pragma: no cover - Windows only
        access = self.READ_CONTROL | self.FILE_READ_ATTRIBUTES
        if read:
            access |= self.GENERIC_READ
        flags = self.FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= self.FILE_FLAG_BACKUP_SEMANTICS
        handle = self.kernel32.CreateFileW(
            str(path),
            access,
            self.FILE_SHARE_READ
            | self.FILE_SHARE_WRITE
            | (0 if lock_rename else self.FILE_SHARE_DELETE),
            None,
            self.OPEN_EXISTING,
            flags,
            None,
        )
        if handle == self.invalid_handle:
            self._raise_last_error("CreateFileW(open existing)")
        return int(handle)

    def validate_handle(
        self,
        handle: int,
        *,
        directory: bool,
        exact_acl: bool,
        validate_security: bool = True,
    ) -> _WindowsFileIdentity:  # pragma: no cover - Windows only
        identity, attributes = self._file_information(handle)
        if bool(attributes & self.FILE_ATTRIBUTE_DIRECTORY) != directory:
            raise OSError("Windows CAN path has the wrong object type")
        if not directory and identity.links != 1:
            raise OSError("Windows CAN private file must have exactly one filesystem link")
        if validate_security:
            self._validate_security(handle, exact=exact_acl)
        return identity

    def validate_directory(
        self, path: Path, *, exact_acl: bool, validate_security: bool = True
    ) -> None:
        # pragma: no cover - Windows only
        handle = self.open_existing(path, directory=True)
        try:
            self.validate_handle(
                handle,
                directory=True,
                exact_acl=exact_acl,
                validate_security=validate_security,
            )
        finally:
            self.close(handle)

    def create_directory(self, path: Path) -> None:  # pragma: no cover - Windows only
        if not self.kernel32.CreateDirectoryW(
            str(path), self.ctypes.byref(self.security_attributes)
        ):
            self._raise_last_error("CreateDirectoryW")

    def create_file(self, path: Path) -> int:  # pragma: no cover - Windows only
        handle = self.kernel32.CreateFileW(
            str(path),
            self.GENERIC_WRITE | self.GENERIC_READ | self.READ_CONTROL | self.FILE_READ_ATTRIBUTES,
            self.FILE_SHARE_READ | self.FILE_SHARE_DELETE,
            self.ctypes.byref(self.security_attributes),
            self.CREATE_NEW,
            self.FILE_ATTRIBUTE_NORMAL
            | self.FILE_FLAG_OPEN_REPARSE_POINT
            | self.FILE_FLAG_WRITE_THROUGH,
            None,
        )
        if handle == self.invalid_handle:
            self._raise_last_error("CreateFileW(create private)")
        numeric = int(handle)
        try:
            self.validate_handle(numeric, directory=False, exact_acl=True)
        except BaseException:
            self.close(numeric)
            raise
        return numeric

    def write_all(self, handle: int, data: bytes) -> None:  # pragma: no cover - Windows only
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + 1024 * 1024]
            buffer = self.ctypes.create_string_buffer(chunk)
            written = self.wintypes.DWORD()
            if not self.kernel32.WriteFile(
                handle, buffer, len(chunk), self.ctypes.byref(written), None
            ):
                self._raise_last_error("WriteFile")
            if written.value <= 0:
                raise OSError("short write while persisting private CAN evidence")
            offset += int(written.value)

    def flush(self, handle: int) -> None:  # pragma: no cover - Windows only
        if not self.kernel32.FlushFileBuffers(handle):
            self._raise_last_error("FlushFileBuffers")

    def close(self, handle: int) -> None:  # pragma: no cover - Windows only
        if not self.kernel32.CloseHandle(handle):
            self._raise_last_error("CloseHandle")

    def delete(self, path: Path) -> None:  # pragma: no cover - Windows only
        if not self.kernel32.DeleteFileW(str(path)):
            get_last_error = getattr(self.ctypes, "get_last_error")
            error = get_last_error()
            if error not in {2, 3}:
                win_error = getattr(self.ctypes, "WinError")
                raise win_error(error, "DeleteFileW")

    def move(self, source: Path, target: Path, *, replace_existing: bool) -> None:
        # pragma: no cover - Windows only
        flags = self.MOVEFILE_WRITE_THROUGH
        if replace_existing:
            flags |= self.MOVEFILE_REPLACE_EXISTING
        if not self.kernel32.MoveFileExW(str(source), str(target), flags):
            self._raise_last_error("MoveFileExW(MOVEFILE_WRITE_THROUGH)")

    def read_bounded(self, handle: int, maximum_bytes: int) -> bytes:
        # pragma: no cover - Windows only
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            amount = min(remaining, 64 * 1024)
            buffer = self.ctypes.create_string_buffer(amount)
            read = self.wintypes.DWORD()
            if not self.kernel32.ReadFile(handle, buffer, amount, self.ctypes.byref(read), None):
                self._raise_last_error("ReadFile")
            if read.value == 0:
                break
            chunks.append(buffer.raw[: read.value])
            remaining -= int(read.value)
        return b"".join(chunks)


_WINDOWS_API: _WindowsApi | None = None


def _windows_api() -> _WindowsApi:
    global _WINDOWS_API
    if not _is_windows():
        raise OSError("Win32 private-file support requested on a non-Windows host")
    if _WINDOWS_API is None:  # pragma: no branch - cached after first Windows use
        _WINDOWS_API = _WindowsApi()
    return _WINDOWS_API


def _validate_windows_directory_ancestry(path: Path, *, final_exact_acl: bool) -> None:
    """Validate every directory component without following reparse points.

    Checking only the final directory is insufficient: an attacker can rename
    a previously validated tree and replace an ancestor with a junction that
    still reaches the original private leaf.  The leaf's identity and DACL
    would remain valid even though the pathname is now redirected.  Walk the
    lexical path from the NTFS volume root and open every component with
    ``FILE_FLAG_OPEN_REPARSE_POINT`` immediately around formal I/O.
    """

    api = _windows_api()
    absolute = Path(os.path.abspath(path))
    volume_root = api.validate_volume(absolute)
    try:
        relative = absolute.relative_to(volume_root)
    except ValueError as exc:
        raise OSError("Windows CAN path escaped its validated NTFS volume") from exc

    if not relative.parts:
        api.validate_directory(
            volume_root,
            exact_acl=final_exact_acl,
            validate_security=True,
        )
        return
    api.validate_directory(volume_root, exact_acl=False, validate_security=False)
    current = volume_root
    for component in relative.parts:
        current = current / component
        final = current == absolute
        api.validate_directory(
            current,
            exact_acl=final and final_exact_acl,
            validate_security=final,
        )


def _ensure_private_directory_windows(path: Path, *, parents: bool) -> None:
    api = _windows_api()
    path = Path(os.path.abspath(path))
    volume_root = api.validate_volume(path)
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        # With parents disabled only the requested child may be absent. Move
        # to its parent before validating existence so the child can actually
        # be created instead of treating its expected absence as an error.
        if not parents:
            cursor = parent
            break
        if parent == cursor:
            break
        cursor = parent
    if not cursor.exists():
        raise OSError(f"Windows CAN private directory parent does not exist: {cursor}")

    # Check every existing path component by a no-reparse-point handle.  The
    # closest existing directory is the caller-owned trust root, so its DACL is
    # additionally checked for untrusted writers.  Volume-root ACL policy is
    # intentionally not confused with the caller-owned-root policy.
    chain: list[Path] = []
    component = cursor
    while component != volume_root and component.parent != component:
        chain.append(component)
        component = component.parent
    for candidate in reversed(chain):
        api.validate_directory(
            candidate,
            exact_acl=False,
            validate_security=candidate == cursor,
        )
    api.validate_directory(cursor, exact_acl=False)

    for candidate in reversed(missing):
        api.create_directory(candidate)
        api.validate_directory(candidate, exact_acl=True)

    # Existing final directories must have been created by this seam; merely
    # being owner-writable is not a sufficient substitute for the protected
    # object DACL used for evidence children.
    if not missing:
        api.validate_directory(path, exact_acl=True)


def ensure_private_directory(path: Path, *, parents: bool = False) -> None:
    """Ensure one private, durable CAN directory.

    POSIX synchronizes the parent directory entry.  Windows creates and checks
    a protected DACL at creation; NTFS metadata publication is subsequently
    made write-through when evidence files are moved into the directory.
    """

    absolute = Path(os.path.abspath(path))
    if _is_windows():
        _ensure_private_directory_windows(absolute, parents=parents)
    else:
        _ensure_private_directory_posix(absolute, parents=parents)


def _temporary_path(parent: Path, prefix: str, suffix: str) -> Path:
    for _ in range(128):
        candidate = parent / f"{prefix}{secrets.token_hex(16)}{suffix}"
        if not candidate.exists():
            return candidate
    raise OSError("cannot allocate a unique private CAN temporary path")


def _read_private_posix(path: Path, maximum_bytes: int) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    parent_descriptor = _open_directory_chain_posix(path.parent)
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("private CAN path is not a regular file")
        if before.st_nlink != 1:
            raise OSError("private CAN file must have exactly one filesystem link")
        if stat.S_IMODE(before.st_mode) != PRIVATE_FILE_MODE:
            raise OSError("private CAN file does not have mode 0600")
        if not _posix_owner_is_current(before):
            raise OSError("private CAN file has a different owner")
        if before.st_size > maximum_bytes:
            raise OSError("private CAN file exceeds its size bound")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(data) > maximum_bytes or (
            before.st_dev,
            before.st_ino,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OSError("private CAN file changed while it was being read")
        return data
    finally:
        os.close(descriptor)


def read_regular_file_no_symlinks(path: Path, *, maximum_bytes: int) -> bytes:
    """Read a bounded profile without following POSIX links or Windows reparse points.

    Windows pins each ancestor against rename while reading the source handle;
    profiles are public configuration, so private evidence ACLs are not required.
    """

    if maximum_bytes < 0:
        raise ValueError("maximum_bytes must be non-negative")
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    inspected: os.stat_result | None = None
    if _is_windows():
        api = _windows_api()
        root = api.validate_volume(absolute)
        handles: list[int] = []
        try:
            current = root
            for component in (None, *absolute.parent.relative_to(root).parts):
                if component is not None:
                    current = current / component
                handle = api.open_existing(current, directory=True, lock_rename=True)
                handles.append(handle)
                api.validate_handle(
                    handle, directory=True, exact_acl=False, validate_security=False
                )
            handle = api.open_existing(absolute, directory=False, read=True, lock_rename=True)
            handles.append(handle)
            before_identity = api.validate_handle(
                handle,
                directory=False,
                exact_acl=False,
                validate_security=False,
            )
            data = api.read_bounded(handle, maximum_bytes)
            after_identity = api.validate_handle(
                handle,
                directory=False,
                exact_acl=False,
                validate_security=False,
            )
            if len(data) > maximum_bytes or before_identity != after_identity:
                raise OSError(
                    "CAN source file changed while it was being read or exceeds its size bound"
                )
            return data
        finally:
            for handle in reversed(handles):
                api.close(handle)
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
        parent_descriptor = _open_directory_chain_posix(absolute.parent)
        try:
            descriptor = os.open(absolute.name, flags, dir_fd=parent_descriptor)
        finally:
            os.close(parent_descriptor)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("CAN source path is not a regular file")
        if inspected is not None and (inspected.st_dev, inspected.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise OSError("CAN source file changed between inspection and open")
        if before.st_size > maximum_bytes:
            raise OSError("CAN source file exceeds its size bound")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(data) > maximum_bytes or _stable_regular_identity(
            before
        ) != _stable_regular_identity(after):
            raise OSError("CAN source file changed while it was being read")
        return data
    finally:
        os.close(descriptor)


def _read_private_windows(path: Path, maximum_bytes: int) -> bytes:
    api = _windows_api()
    _validate_windows_directory_ancestry(path.parent, final_exact_acl=True)
    handle = api.open_existing(path, directory=False, read=True)
    try:
        before = api.validate_handle(handle, directory=False, exact_acl=True)
        if before.size > maximum_bytes:
            raise OSError("private CAN file exceeds its size bound")
        data = api.read_bounded(handle, maximum_bytes)
        after = api.validate_handle(handle, directory=False, exact_acl=True)
        if len(data) > maximum_bytes or before != after:
            raise OSError("private CAN file changed while it was being read")
        _validate_windows_directory_ancestry(path.parent, final_exact_acl=True)
        return data
    finally:
        api.close(handle)


def read_private_file(path: Path, *, maximum_bytes: int) -> bytes:
    """Read one bounded private regular file through a validated handle."""

    if maximum_bytes < 0:
        raise ValueError("maximum_bytes must be non-negative")
    absolute = Path(os.path.abspath(path))
    if _is_windows():
        return _read_private_windows(absolute, maximum_bytes)
    return _read_private_posix(absolute, maximum_bytes)


def _write_atomic_windows(path: Path, data: bytes, *, replace_existing: bool) -> None:
    api = _windows_api()
    _validate_windows_directory_ancestry(path.parent, final_exact_acl=True)
    temporary = _temporary_path(path.parent, f".{path.stem}-", ".tmp")
    created_handle = api.create_file(temporary)
    handle: int | None = created_handle
    identity: _WindowsFileIdentity | None = None
    try:
        api.write_all(created_handle, data)
        api.flush(created_handle)
        identity = api.validate_handle(created_handle, directory=False, exact_acl=True)
        # Close failure is ambiguous; relinquish ownership before the call so
        # the exception path never retries a possibly recycled HANDLE.
        handle = None
        api.close(created_handle)
        _validate_windows_directory_ancestry(path.parent, final_exact_acl=True)
        api.move(temporary, path, replace_existing=replace_existing)
        _validate_windows_directory_ancestry(path.parent, final_exact_acl=True)
        reopened = api.open_existing(path, directory=False, read=False)
        try:
            published = api.validate_handle(reopened, directory=False, exact_acl=True)
            if identity != published:
                raise OSError("published Windows CAN file identity changed")
        finally:
            api.close(reopened)
    except BaseException:
        if handle is not None:
            try:
                api.close(handle)
            except OSError:
                pass
        try:
            api.delete(temporary)
        except OSError:
            pass
        raise


def _write_immutable_posix(path: Path, data: bytes, maximum_bytes: int) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_descriptor = _open_directory_chain_posix(path.parent)
    try:
        descriptor = os.open(
            path.name,
            flags,
            PRIVATE_FILE_MODE,
            dir_fd=parent_descriptor,
        )
    except FileExistsError:
        os.close(parent_descriptor)
        existing = read_private_file(path, maximum_bytes=maximum_bytes)
        if not hmac.compare_digest(existing, data):
            raise OSError("existing immutable CAN file content does not match")
        return
    except BaseException:
        os.close(parent_descriptor)
        raise
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            fchmod = getattr(os, "fchmod", None)
            if fchmod is None:
                raise OSError("POSIX private-file support requires fchmod")
            else:
                fchmod(handle.fileno(), PRIVATE_FILE_MODE)
            os.fsync(handle.fileno())
        os.fsync(parent_descriptor)
    except BaseException:
        try:
            os.unlink(path.name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(parent_descriptor)


def _write_immutable_windows(path: Path, data: bytes, maximum_bytes: int) -> None:
    try:
        _write_atomic_windows(path, data, replace_existing=False)
        return
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror not in {_WindowsApi.ERROR_FILE_EXISTS, _WindowsApi.ERROR_ALREADY_EXISTS}:
            raise
    existing = read_private_file(path, maximum_bytes=maximum_bytes)
    if not hmac.compare_digest(existing, data):
        raise OSError("existing immutable CAN file content does not match")


def write_immutable_private_file(path: Path, data: bytes, *, maximum_bytes: int) -> None:
    """Create or securely reuse one content-addressed private file."""

    if len(data) > maximum_bytes:
        raise OSError("immutable private CAN file exceeds its size bound")
    absolute = Path(os.path.abspath(path))
    if _is_windows():
        _write_immutable_windows(absolute, data, maximum_bytes)
    else:
        _write_immutable_posix(absolute, data, maximum_bytes)


class PrivateAppendWriter:
    """One retained private handle for the append-only health chain."""

    def __init__(self, path: Path) -> None:
        self.path = Path(os.path.abspath(path))
        self.descriptor: int | None = None
        self._parent_descriptor: int | None = None
        self._temporary: Path | None = None

    def _open_posix(self) -> None:
        parent_descriptor = _open_directory_chain_posix(self.path.parent)
        try:
            self.descriptor = os.open(
                self.path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                PRIVATE_FILE_MODE,
                dir_fd=parent_descriptor,
            )
        except BaseException:
            os.close(parent_descriptor)
            raise
        self._parent_descriptor = parent_descriptor

    def _open_windows(self) -> None:
        api = _windows_api()
        _validate_windows_directory_ancestry(self.path.parent, final_exact_acl=True)
        self._temporary = _temporary_path(self.path.parent, ".health-", ".tmp")
        self.descriptor = api.create_file(self._temporary)

    def append(self, data: bytes, *, first: bool) -> None:
        if first:
            if self.descriptor is not None:
                raise OSError("private CAN append writer is already open")
            if _is_windows():
                self._open_windows()
            else:
                self._open_posix()
        if self.descriptor is None:
            raise OSError("private CAN append writer is not open")
        if _is_windows():
            api = _windows_api()
            if not first:
                _validate_windows_directory_ancestry(self.path.parent, final_exact_acl=True)
            api.write_all(self.descriptor, data)
            api.flush(self.descriptor)
            if first:
                assert self._temporary is not None
                identity = api.validate_handle(self.descriptor, directory=False, exact_acl=True)
                _validate_windows_directory_ancestry(self.path.parent, final_exact_acl=True)
                api.move(self._temporary, self.path, replace_existing=False)
                self._temporary = None
                _validate_windows_directory_ancestry(self.path.parent, final_exact_acl=True)
                reopened = api.open_existing(self.path, directory=False, read=False)
                try:
                    if identity != api.validate_handle(reopened, directory=False, exact_acl=True):
                        raise OSError("published Windows CAN health-file identity changed")
                finally:
                    api.close(reopened)
            else:
                _validate_windows_directory_ancestry(self.path.parent, final_exact_acl=True)
        else:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is None:  # pragma: no cover - POSIX contract guard
                raise OSError("POSIX private-file support requires fchmod")
            fchmod(self.descriptor, PRIVATE_FILE_MODE)
            view = memoryview(data)
            while view:
                written = os.write(self.descriptor, view)
                if written <= 0:
                    raise OSError("short write while persisting CAN health evidence")
                view = view[written:]
            os.fsync(self.descriptor)
            if first:
                parent_descriptor = self._parent_descriptor
                if parent_descriptor is None:
                    raise OSError("private CAN append writer lost its parent directory handle")
                os.fsync(parent_descriptor)
                os.close(parent_descriptor)
                self._parent_descriptor = None

    def close(self) -> None:
        descriptor = self.descriptor
        self.descriptor = None
        if descriptor is None:
            return
        if _is_windows():
            _windows_api().close(descriptor)
        else:
            os.close(descriptor)
            parent_descriptor = self._parent_descriptor
            self._parent_descriptor = None
            if parent_descriptor is not None:
                os.close(parent_descriptor)

    def abort(self) -> None:
        try:
            self.close()
        finally:
            temporary = self._temporary
            self._temporary = None
            if temporary is not None:
                try:
                    if _is_windows():
                        _windows_api().delete(temporary)
                    else:
                        temporary.unlink()
                except OSError:
                    pass
