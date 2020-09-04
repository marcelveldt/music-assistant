"""Generic Models and helpers for plugins."""

from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, List, Optional

from music_assistant.models.config_entry import ConfigEntry

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class ProviderType(str, Enum):
    """Enum with plugin types."""

    MUSIC_PROVIDER = "music_provider"
    PLAYER_PROVIDER = "player_provider"
    GENERIC = "generic"


@dataclass
class Provider:
    """Base model for a provider/plugin."""

    type: ProviderType = ProviderType.GENERIC
    mass: "MusicAssistant" = None
    available: bool = False

    @property
    @abstractmethod
    def id(self) -> str:
        """Return provider ID for this provider."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return provider Name for this provider."""

    @property
    @abstractmethod
    def config_entries(self) -> List[ConfigEntry]:
        """Return Config Entries for this provider."""

    @abstractmethod
    async def async_on_start(self) -> bool:
        """Called on startup.
        Handle initialization of the provider based on config.
        Return bool if start was succesfull"""
        raise NotImplementedError

    @abstractmethod
    async def async_on_stop(self):
        """Called on shutdown. Handle correct close/cleanup of the provider on exit."""
        raise NotImplementedError

    async def async_on_reload(self):
        """Called on reload. Handle configuration changes for this provider."""
        await self.async_on_stop()
        await self.async_on_start()
