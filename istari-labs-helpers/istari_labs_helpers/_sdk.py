"""Internal v2 / v3 client pairing for :class:`IstariPlatform`.

Callers interact with entity views and queries; this module hides which SDK
surface (v2 ``Client`` vs v3 ``V3Client``) backs a given operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from istari_digital_client.client import Client
from istari_digital_client.v3_client import V3Client

if TYPE_CHECKING:
    from istari_digital_client.configuration import Configuration


@dataclass
class SdkClients:
    """Paired v2 and v3 clients sharing one :class:`Configuration`."""

    v2: Client
    _v3: V3Client | None = None

    @classmethod
    def from_config(cls, config: Configuration) -> SdkClients:
        return cls(v2=Client(config), _v3=V3Client(config))

    @classmethod
    def from_v2(cls, client: Client) -> SdkClients:
        """Wrap an existing v2 client; v3 is created lazily on first access."""
        return cls(v2=client)

    @property
    def v3(self) -> V3Client:
        if self._v3 is None:
            cfg = self.v2.configuration
            self._v3 = V3Client(cfg)
        return self._v3

    @property
    def config(self) -> Configuration:
        return self.v2.configuration
