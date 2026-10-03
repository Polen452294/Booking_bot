"""Private local registry and crash-safe writes (one operator per registry)."""

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class DeploymentError(RuntimeError):
    """Messages are safe for operator output; never attach raw subprocess errors."""


def private_directory(path: Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise DeploymentError("Registry directories must not be links")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "nt":
        # chmod on Windows does not remove inherited access for other users.
        quoted = str(path).replace("'", "''")
        script = (
            "$ErrorActionPreference='Stop';"
            "$acl=New-Object System.Security.AccessControl.DirectorySecurity;"
            "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User;"
            "$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false);"
            "$rule=[System.Security.AccessControl.FileSystemAccessRule]::new"
            "($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow');"
            "$acl.AddAccessRule($rule);"
            f"[System.IO.Directory]::SetAccessControl('{quoted}', $acl)"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=30,
        )
        if result.returncode:
            raise DeploymentError("Cannot set private Windows registry ACL")
    else:
        path.chmod(0o700)


def atomic_write(path: Path, content: str, *, public_config: bool = False) -> None:
    if path.is_symlink():
        raise DeploymentError("Registry files must not be symlinks")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            # Config is mounted alone for UID 10001. Its host parent stays 0700.
            os.chmod(temporary, 0o644 if public_config else 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    try:
        if path.is_symlink():
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise DeploymentError("Missing or corrupt deployment state; files were preserved") from None


def private_permissions(paths: list[Path]) -> bool:
    if any(not path.exists() or path.is_symlink() or path.is_junction() for path in paths):
        return False
    if os.name != "nt":
        return all(path.stat().st_mode & 0o077 == 0 for path in paths)
    quoted = ",".join("'" + str(path).replace("'", "''") + "'" for path in paths)
    script = (
        "$ErrorActionPreference='Stop';"
        "$allowed=@([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value,"
        "'S-1-5-18','S-1-5-32-544');"
        f"foreach($p in @({quoted})){{"
        "$acl=if([System.IO.Directory]::Exists($p)){[System.IO.Directory]::GetAccessControl($p)}"
        "else{[System.IO.File]::GetAccessControl($p)};"
        "foreach($r in $acl.GetAccessRules($true,$true,"
        "[System.Security.Principal.SecurityIdentifier]))"
        "{if($r.AccessControlType -eq 'Allow' -and $r.IdentityReference.Value -notin $allowed)"
        "{exit 1}}};exit 0"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        timeout=30,
    )
    return result.returncode == 0


@contextmanager
def registry_lock(root: Path) -> Iterator[None]:
    private_directory(root)
    path = root / ".lock"
    if path.is_symlink():
        raise DeploymentError("Registry lock must not be a symlink")
    with path.open("a+b") as stream:
        if os.name != "nt":
            path.chmod(0o600)
        stream.seek(0)
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DeploymentError("Registry busy: another bookingctl command is running") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def operation_lock(root: Path, directory: Path) -> Iterator[None]:
    """Common backup/restore/release lock; registry lock also excludes legacy config writers."""
    if directory.parent != root or not directory.is_dir():
        raise DeploymentError("Operation requires an existing deployment in this registry")
    # Always acquire in this order, so concurrent commands cannot deadlock.
    with registry_lock(root), registry_lock(directory):
        yield
