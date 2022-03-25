"""Typing helper."""

from typing import TYPE_CHECKING, Any, Callable, Optional, Set, Tuple

from music_assistant.constants import EventType

# pylint: disable=invalid-name
if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.config.models import ConfigSubItem
    from music_assistant.music.models import MediaType
    from music_assistant.player_queue.models import (
        PlayerQueue,
        QueueItem,
        StreamDetails,
        StreamType,
    )
    from music_assistant.players.models import Player

else:
    MusicAssistant = "MusicAssistant"
    QueueItem = "QueueItem"
    PlayerQueue = "PlayerQueue"
    StreamDetails = "StreamDetails"
    Player = "Player"
    ConfigSubItem = "ConfigSubItem"
    MediaType = "MediaType"
    StreamType = "StreamType"


QueueItems = Set[QueueItem]
Players = Set[Player]

OptionalInt = Optional[int]
OptionalStr = Optional[str]

EventDetails = Any | None
EventCallBackType = Callable[[EventType, EventDetails], None]
EventSubscriptionType = Tuple[EventCallBackType, "Optional[Tuple[EventType]]"]
