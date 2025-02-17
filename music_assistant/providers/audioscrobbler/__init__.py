"""Allows scrobbling of tracks with the help of PyLast."""

import logging
import time
from typing import TYPE_CHECKING

import pylast
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import ConfigEntryType, EventType, PlayerState
from music_assistant_models.errors import LoginFailed, SetupFailedError
from music_assistant_models.event import MassEvent
from music_assistant_models.media_items import Album, is_track
from music_assistant_models.provider import ProviderManifest

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track
    from music_assistant_models.player_queue import PlayerQueue

from music_assistant import MusicAssistant
from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    provider = ScrobblerProvider(mass, manifest, config)
    pylast.logger.setLevel(provider.logger.level)
    if provider.logger.level == logging.DEBUG:
        # httpcore is quite spammy without providing useful information 99% of the time
        logging.getLogger("httpcore").setLevel(logging.INFO)

    return provider


class ScrobblerProvider(PluginProvider):
    """Plugin provider to support scrobbling of tracks."""

    _network: pylast._Network = None
    _currently_playing: str = None
    _processing_queue_update = False

    def _get_network_config(self) -> dict[str, ConfigValueType]:
        return {
            CONF_API_KEY: self.config.get_value(CONF_API_KEY),
            CONF_API_SECRET: self.config.get_value(CONF_API_SECRET),
            CONF_PROVIDER: self.config.get_value(CONF_PROVIDER),
            CONF_USERNAME: self.config.get_value(CONF_USERNAME),
            CONF_SESSION_KEY: self.config.get_value(CONF_SESSION_KEY),
        }

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()

        if not self.config.get_value(CONF_SESSION_KEY):
            self.logger.info("No session key available")
            return

        self._network = _get_network(self._get_network_config())

        # subscribe to internal events
        self.mass.subscribe(self._on_mass_queue_updated, EventType.QUEUE_UPDATED)
        self.mass.subscribe(self._on_mass_media_item_played, EventType.MEDIA_ITEM_PLAYED)
        self.logger.debug("subscribed to events")

    async def _on_mass_queue_updated(self, event: MassEvent) -> None:
        """Player has updated, update nowPlaying."""
        if self._network is None:
            self.logger.error("no network available during _on_mass_queue_updated")
            return

        if self._processing_queue_update:
            return

        queue: PlayerQueue = event.data
        if queue.state != PlayerState.PLAYING or not is_track(queue.current_item.media_item):
            self.logger.debug("queue update ignored, no track currently playing")
            self._currently_playing = None
            return

        track: Track = queue.current_item.media_item
        if track.uri == self._currently_playing:
            self.logger.debug(
                f"queue update ignored, track {track.uri} already marked as 'now playing'"
            )
            return

        self._processing_queue_update = True

        def update_now_playing() -> None:
            try:
                self._network.update_now_playing(
                    track.artist_str,
                    track.name,
                    track.album.name if track.album else None,
                    track.album.artist_str if isinstance(track.album, Album) else None,
                    track.duration,
                    track.track_number,
                    track.mbid,
                )
                self.logger.debug(f"track {track.uri} marked as 'now playing'")
                self._currently_playing = track.uri
            except Exception as err:
                self.logger.exception(err)
            finally:
                self._processing_queue_update = False

        self.mass.loop.run_in_executor(None, update_now_playing)

    async def _on_mass_media_item_played(self, event: MassEvent) -> None:
        """Media item has finished playing, we'll scrobble the track."""
        if self._network is None:
            self.logger.error("no network available during _on_mass_media_item_played")
            return

        item = await self.mass.music.get_item_by_uri(event.data["media_item"])
        if not is_track(item):
            self.logger.debug(f"track {event.data['media_item']} skipped, as it is not a track")
            return

        track: Track = item

        if event.data["seconds_played"] / track.duration < 0.5:
            self.logger.debug(f"track {track.uri} ignored, playtime was not long enough")
            return

        def scrobble() -> None:
            try:
                self._network.scrobble(
                    track.artist_str,
                    track.name,
                    time.time(),
                    track.album.name if track.album else None,
                    track.album.artist_str if track.album else None,
                    track.track_number,
                    track.duration,
                    mbid=track.mbid,
                )
            except Exception as err:
                self.logger.exception(err)

        self.logger.debug("running scrobble() in executor")
        self.mass.loop.run_in_executor(None, scrobble)


# configuration keys
CONF_API_KEY = "_api_key"
CONF_API_SECRET = "_api_secret"
CONF_SESSION_KEY = "_api_session_key"
CONF_USERNAME = "_username"
CONF_PROVIDER = "_provider"

# configuration actions
CONF_ACTION_AUTH = "_auth"

# available networks
CONF_OPTION_LASTFM = "lastfm"
CONF_OPTION_LIBREFM = "librefm"


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    logger = logging.getLogger(MASS_LOGGER_NAME).getChild("audioscrobbler")

    provider: str = values.get(CONF_PROVIDER)
    if values is None or not values.get(CONF_PROVIDER):
        provider = CONF_OPTION_LASTFM

    # collect all config entries to show
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key=CONF_PROVIDER,
            type=ConfigEntryType.STRING,
            label="Provider",
            required=True,
            description="The endpoint to use, defaults to Last.fm",
            options=(
                ConfigValueOption(title="Last.FM", value=CONF_OPTION_LASTFM),
                ConfigValueOption(title="LibreFM", value=CONF_OPTION_LIBREFM),
            ),
            default_value=provider,
            value=provider,
        ),
        ConfigEntry(
            key=CONF_API_KEY,
            type=ConfigEntryType.SECURE_STRING,
            label="API Key",
            required=True,
            value=values.get(CONF_API_KEY) if values else None,
        ),
        ConfigEntry(
            key=CONF_API_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            label="Shared secret",
            required=True,
            value=values.get(CONF_API_SECRET) if values else None,
        ),
    ]

    if action == CONF_ACTION_AUTH:
        async with AuthenticationHelper(mass, str(values["session_id"])) as auth_helper:
            network = _get_network(values)
            skg = pylast.SessionKeyGenerator(network)

            # pylast says it does web auth, but actually does desktop auth
            # so we need to do some URL juggling ourselves
            # to get a proper web auth flow with a callback
            url = (
                f"{network.homepage}/api/auth/"
                f"?api_key={network.api_key}"
                f"&cb={auth_helper.callback_url}"
            )

            logger.info("authenticating on %s", url)
            response = await auth_helper.authenticate(url)
            if not response["token"]:
                raise LoginFailed(f"no token available in {provider} response")

            session_key, username = skg.get_web_auth_session_key_username(
                url, str(response["token"])
            )
            values[CONF_USERNAME] = username
            values[CONF_SESSION_KEY] = session_key

            entries += [
                ConfigEntry(
                    key="save_reminder",
                    type=ConfigEntryType.ALERT,
                    required=False,
                    default_value=None,
                    label=(
                        f"Successfully logged in as {username},",
                        "don't forget to hit save to complete the setup",
                    ),
                ),
            ]

    if values is None or not values.get(CONF_SESSION_KEY):
        # unable to use the encrypted values during an action
        # so we make sure fresh credentials need to be entered
        values[CONF_API_KEY] = None
        values[CONF_API_SECRET] = None
        entries += [
            ConfigEntry(
                key=CONF_ACTION_AUTH,
                type=ConfigEntryType.ACTION,
                label=f"Authorize with {provider}",
                action=CONF_ACTION_AUTH,
            ),
        ]

    entries += [
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Logged in user",
            hidden=True,
            value=values.get(CONF_USERNAME) if values else None,
        ),
        ConfigEntry(
            key=CONF_SESSION_KEY,
            type=ConfigEntryType.SECURE_STRING,
            label="Session key",
            hidden=True,
            required=False,
            value=values.get(CONF_SESSION_KEY) if values else None,
        ),
    ]

    return tuple(entries)


def _get_network(config: dict[str, ConfigValueType]) -> pylast._Network:
    key = config.get(CONF_API_KEY)
    secret = config.get(CONF_API_SECRET)
    session_key = config.get(CONF_SESSION_KEY)

    assert key
    assert key != SECURE_STRING_SUBSTITUTE
    assert secret
    assert secret != SECURE_STRING_SUBSTITUTE

    if not key or not secret:
        raise SetupFailedError("API Key and Secret need to be set")

    match config.get(CONF_PROVIDER).lower():
        case "lastfm":
            return pylast.LastFMNetwork(
                key, secret, username=config.get(CONF_USERNAME), session_key=session_key
            )
        case "librefm":
            return pylast.LibreFMNetwork(
                key, secret, username=config.get(CONF_USERNAME), session_key=session_key
            )
