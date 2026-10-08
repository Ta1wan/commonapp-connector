"""Builds connector components from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .sinks import CsvSink, JsonlSink, Sink, WebhookSink
from .sources import LocalDirectorySource, SFTPSource
from .sources.base import Source


@dataclass
class Settings:
    source: str = "local"            # "local" or "sftp"
    local_dir: str = "./inbox"
    file_pattern: str = "*.txt"
    sftp_host: str = ""
    sftp_port: int = 22
    sftp_user: str = ""
    sftp_password: str | None = None
    sftp_key_path: str | None = None
    sftp_remote_dir: str = "/"
    sftp_known_hosts: str | None = None
    field_map_path: str | None = None
    state_path: str = "./.state/commonapp.json"
    jsonl_path: str | None = None
    csv_dir: str | None = None
    webhook_url: str | None = None
    webhook_token: str | None = None

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        def get(name, default=None):
            return env.get(f"COMMONAPP_{name}", default) or default

        return cls(
            source=get("SOURCE", "local"),
            local_dir=get("LOCAL_DIR", "./inbox"),
            file_pattern=get("FILE_PATTERN", "*.txt"),
            sftp_host=get("SFTP_HOST", ""),
            sftp_port=int(get("SFTP_PORT", "22")),
            sftp_user=get("SFTP_USER", ""),
            sftp_password=get("SFTP_PASSWORD"),
            sftp_key_path=get("SFTP_KEY_PATH"),
            sftp_remote_dir=get("SFTP_REMOTE_DIR", "/"),
            sftp_known_hosts=get("SFTP_KNOWN_HOSTS"),
            field_map_path=get("FIELD_MAP"),
            state_path=get("STATE_PATH", "./.state/commonapp.json"),
            jsonl_path=get("JSONL_PATH"),
            csv_dir=get("CSV_DIR"),
            webhook_url=get("WEBHOOK_URL"),
            webhook_token=get("WEBHOOK_TOKEN"),
        )

    def build_source(self) -> Source:
        if self.source == "local":
            return LocalDirectorySource(self.local_dir, self.file_pattern)
        if self.source == "sftp":
            if not (self.sftp_host and self.sftp_user):
                raise ValueError("COMMONAPP_SFTP_HOST and COMMONAPP_SFTP_USER are required for sftp")
            return SFTPSource(
                self.sftp_host, self.sftp_user, password=self.sftp_password,
                key_path=self.sftp_key_path, remote_dir=self.sftp_remote_dir,
                pattern=self.file_pattern, port=self.sftp_port, known_hosts=self.sftp_known_hosts,
            )
        raise ValueError(f"Unknown source {self.source!r}; expected 'local' or 'sftp'")

    def build_sinks(self) -> list[Sink]:
        sinks: list[Sink] = []
        if self.jsonl_path:
            sinks.append(JsonlSink(self.jsonl_path))
        if self.csv_dir:
            sinks.append(CsvSink(self.csv_dir))
        if self.webhook_url:
            sinks.append(WebhookSink(self.webhook_url, self.webhook_token))
        if not sinks:
            raise ValueError("Configure at least one sink: COMMONAPP_JSONL_PATH, COMMONAPP_CSV_DIR or COMMONAPP_WEBHOOK_URL")
        return sinks
