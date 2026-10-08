from __future__ import annotations

import fnmatch
import stat
from pathlib import Path
from typing import Iterator

from .base import ExportFile, Source


class SFTPSource(Source):
    """Pulls exports from an SFTP drop, such as Common App's SDS delivery.

    Requires the ``sftp`` extra (paramiko).
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str | None = None,
        key_path: str | None = None,
        remote_dir: str = "/",
        pattern: str = "*.txt",
        port: int = 22,
        known_hosts: str | None = None,
    ):
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError("SFTP support needs paramiko: pip install 'commonapp-connector[sftp]'") from exc

        self.remote_dir = remote_dir.rstrip("/") or "/"
        self.pattern = pattern
        self._ssh = paramiko.SSHClient()
        if known_hosts:
            self._ssh.load_host_keys(known_hosts)
        else:
            self._ssh.load_system_host_keys()
        self._ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        self._ssh.connect(
            host, port=port, username=username, password=password, key_filename=key_path,
            allow_agent=False, look_for_keys=key_path is None and password is None,
        )
        self._sftp = self._ssh.open_sftp()

    def list_files(self) -> Iterator[ExportFile]:
        entries = [
            e for e in self._sftp.listdir_attr(self.remote_dir)
            if stat.S_ISREG(e.st_mode or 0) and fnmatch.fnmatch(e.filename, self.pattern)
        ]
        for e in sorted(entries, key=lambda e: (e.st_mtime or 0, e.filename)):
            yield ExportFile(e.filename, f"{e.st_size}:{e.st_mtime}")

    def fetch(self, file: ExportFile, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        local = dest_dir / file.name
        self._sftp.get(f"{self.remote_dir}/{file.name}", str(local))
        return local

    def close(self) -> None:
        self._sftp.close()
        self._ssh.close()
