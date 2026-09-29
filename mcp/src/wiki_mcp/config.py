"""Configuration, read once from the environment."""

import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    #: Where kiwix-serve answers, without a trailing slash.
    kiwix_url: str = "http://kiwix.wiki.svc.cluster.local"
    #: Bearer token every MCP request must carry. None only when auth is explicitly disabled.
    auth_token: str | None = None
    #: Host headers the MCP endpoint answers for (DNS-rebinding protection); empty turns it off.
    allowed_hosts: tuple[str, ...] = ()
    port: int = 8000
    #: How much of a document `read_library` returns per call.
    page_chars: int = 8000

    @classmethod
    def from_env(cls) -> Config:
        token = os.environ.get("MCP_AUTH_TOKEN", "").strip() or None
        insecure = os.environ.get("MCP_INSECURE_NO_AUTH", "") == "1"
        if token is None and not insecure:
            raise ConfigError(
                "MCP_AUTH_TOKEN is not set (or set MCP_INSECURE_NO_AUTH=1 for local testing)."
            )
        hosts = tuple(
            h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
        )
        return cls(
            kiwix_url=os.environ.get("KIWIX_URL", cls.kiwix_url).rstrip("/"),
            auth_token=token,
            allowed_hosts=hosts,
            port=int(os.environ.get("PORT", cls.port)),
            page_chars=int(os.environ.get("READ_PAGE_CHARS", cls.page_chars)),
        )
