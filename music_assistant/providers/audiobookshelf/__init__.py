"""Audiobookshelf provider for Music Assistant.

Audiobookshelf is abbreviated ABS here.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from copy import deepcopy
from typing import TYPE_CHECKING

import aioaudiobookshelf as aioabs
from aioaudiobookshelf.client.items import LibraryItemExpandedBook as AbsLibraryItemExpandedBook
from aioaudiobookshelf.client.items import (
    LibraryItemExpandedPodcast as AbsLibraryItemExpandedPodcast,
)
from aioaudiobookshelf.exceptions import ApiError as AbsApiError
from aioaudiobookshelf.exceptions import LoginError as AbsLoginError
from aioaudiobookshelf.schema.library import (
    LibraryItemMinifiedBook as AbsLibraryItemMinifiedBook,
)
from aioaudiobookshelf.schema.library import (
    LibraryItemMinifiedPodcast as AbsLibraryItemMinifiedPodcast,
)
from aioaudiobookshelf.schema.library import LibraryMediaType as AbsLibraryMediaType
from aioaudiobookshelf.schema.session import DeviceInfo
from aioaudiobookshelf.schema.session import PlaybackSessionExpanded as AbsPlaybackSessionExpanded
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.audiobookshelf.parsers import (
    parse_audiobook,
    parse_podcast,
    parse_podcast_episode,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Audiobook, Podcast, PodcastEpisode
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
# optionally hide podcasts with no episodes
CONF_HIDE_EMPTY_PODCASTS = "hide_empty_podcasts"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return Audiobookshelf(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            label="Server",
            required=True,
            description="The url of the Audiobookshelf server to connect to.",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
            description="The username to authenticate to the remote server.",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            description="The password to authenticate to the remote server.",
        ),
        ConfigEntry(
            key=CONF_VERIFY_SSL,
            type=ConfigEntryType.BOOLEAN,
            label="Verify SSL",
            required=False,
            description="Whether or not to verify the certificate of SSL/TLS connections.",
            category="advanced",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_HIDE_EMPTY_PODCASTS,
            type=ConfigEntryType.BOOLEAN,
            label="Hide empty podcasts.",
            required=False,
            description="This will skip podcasts with no episodes associated.",
            category="advanced",
            default_value=False,
        ),
    )


class Audiobookshelf(MusicProvider):
    """Audiobookshelf MusicProvider."""

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Features supported by this Provider."""
        return {
            ProviderFeature.LIBRARY_PODCASTS,
            ProviderFeature.LIBRARY_AUDIOBOOKS,
            ProviderFeature.BROWSE,
        }

    async def handle_async_init(self) -> None:
        """Pass config values to client and initialize."""
        base_url = str(self.config.get_value(CONF_URL))
        username = str(self.config.get_value(CONF_USERNAME))
        password = str(self.config.get_value(CONF_PASSWORD))
        verify_ssl = bool(self.config.get_value(CONF_VERIFY_SSL))
        session_config = aioabs.SessionConfiguration(
            session=self.mass.http_session, url=base_url, verify_ssl=verify_ssl, logger=self.logger
        )
        try:
            self._client = await aioabs.get_user_client(
                session_config=session_config, username=username, password=password
            )
        except AbsLoginError:
            raise LoginFailed(f"Login to abs instance at {base_url} failed.")

        # this will be provided when creating sessions or receive already opened sessions
        self.device_info = DeviceInfo(
            device_id=self.instance_id,
            client_name="Music Assistant",
            client_version=self.mass.version,
            manufacturer="",
            model=self.mass.server_id,
        )

        # store library ids and session ids.
        self.libraries_book_ids: dict[str, str] = {}  # lib_id: lib_name
        self.libraries_podcast_ids: dict[str, str] = {}
        self.session_ids: set[str] = set()

        self.logger.debug(f"Our playback session device_id is {self.instance_id}")

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        await self._close_all_playback_sessions()
        await self._client.logout()

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        return False

    async def sync_library(self, media_types: tuple[MediaType, ...]) -> None:
        """Obtain audiobook library ids and podcast library ids."""
        libraries = await self._client.get_all_libraries()
        for library in libraries:
            if library.media_type == AbsLibraryMediaType.BOOK:
                self.libraries_book_ids[library.id_] = library.name
            elif library.media_type == AbsLibraryMediaType.PODCAST:
                self.libraries_podcast_ids[library.id_] = library.name
        await super().sync_library(media_types=media_types)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library/subscribed podcasts from the provider.

        Minified podcast information is enough.
        """
        for pod_lib_id in self.libraries_podcast_ids:
            async for response in self._client.get_library_items(library_id=pod_lib_id):
                if not response.results:
                    break
                for abs_podcast in response.results:
                    if type(abs_podcast) is not AbsLibraryItemMinifiedPodcast:
                        raise RuntimeError("Unexpected type of podcast.")
                    mass_podcast = parse_podcast(
                        abs_podcast=abs_podcast,
                        lookup_key=self.lookup_key,
                        domain=self.domain,
                        instance_id=self.instance_id,
                        token=self._client.token,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    )
                    if (
                        bool(self.config.get_value(CONF_HIDE_EMPTY_PODCASTS))
                        and mass_podcast.total_episodes == 0
                    ):
                        continue
                    yield mass_podcast

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get single podcast.

        Basis information is sufficient.
        """
        abs_podcast = await self._client.get_library_item_podcast(
            podcast_id=prov_podcast_id, expanded=False
        )
        return parse_podcast(
            abs_podcast=abs_podcast,
            lookup_key=self.lookup_key,
            domain=self.domain,
            instance_id=self.instance_id,
            token=self._client.token,
            base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
        )

    async def get_podcast_episodes(self, prov_podcast_id: str) -> list[PodcastEpisode]:
        """Get all podcast episodes of podcast.

        Adds progress information.
        """
        abs_podcast = await self._client.get_library_item_podcast(
            podcast_id=prov_podcast_id, expanded=True
        )
        if type(abs_podcast) is not AbsLibraryItemExpandedPodcast:
            raise RuntimeError("Podcast has wrong type.")
        episode_list = []
        episode_cnt = 1
        for abs_episode in abs_podcast.media.episodes:
            progress = await self._client.get_my_media_progress(
                item_id=prov_podcast_id, episode_id=abs_episode.id_
            )
            mass_episode = parse_podcast_episode(
                episode=abs_episode,
                prov_podcast_id=prov_podcast_id,
                fallback_episode_cnt=episode_cnt,
                lookup_key=self.lookup_key,
                domain=self.domain,
                instance_id=self.instance_id,
                token=self._client.token,
                base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                media_progress=progress,
            )
            episode_list.append(mass_episode)
            episode_cnt += 1
        return episode_list

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get single podcast episode."""
        prov_podcast_id, e_id = prov_episode_id.split(" ")
        abs_podcast = await self._client.get_library_item_podcast(
            podcast_id=prov_podcast_id, expanded=True
        )
        if type(abs_podcast) is not AbsLibraryItemExpandedPodcast:
            raise RuntimeError("Podcast has wrong type.")
        episode_cnt = 1
        for abs_episode in abs_podcast.media.episodes:
            if abs_episode.id_ == e_id:
                progress = await self._client.get_my_media_progress(
                    item_id=prov_podcast_id, episode_id=abs_episode.id_
                )
                return parse_podcast_episode(
                    episode=abs_episode,
                    prov_podcast_id=prov_podcast_id,
                    fallback_episode_cnt=episode_cnt,
                    lookup_key=self.lookup_key,
                    domain=self.domain,
                    instance_id=self.instance_id,
                    token=self._client.token,
                    base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    media_progress=progress,
                )

            episode_cnt += 1
        raise MediaNotFoundError("Episode not found")

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
        """Get Audiobook libraries.

        Need expanded version for chapters.
        """
        for book_lib_id in self.libraries_book_ids:
            async for response in self._client.get_library_items(library_id=book_lib_id):
                if not response.results:
                    break
                for abs_audiobook in response.results:
                    if type(abs_audiobook) is not AbsLibraryItemMinifiedBook:
                        raise RuntimeError("Unexpected type of book.")
                    abs_book_expanded = await self._client.get_library_item_book(
                        book_id=abs_audiobook.id_, expanded=True
                    )
                    # use expanded version for chapters.
                    if type(abs_book_expanded) is not AbsLibraryItemExpandedBook:
                        raise RuntimeError("Unexpected type of book.")
                    mass_audiobook = parse_audiobook(
                        abs_audiobook=abs_book_expanded,
                        lookup_key=self.lookup_key,
                        domain=self.domain,
                        instance_id=self.instance_id,
                        token=self._client.token,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    )
                    yield mass_audiobook

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get a single audiobook.

        Progress is added here.
        """
        abs_audiobook = await self._client.get_library_item_book(
            book_id=prov_audiobook_id, expanded=True
        )
        if type(abs_audiobook) is not AbsLibraryItemExpandedBook:
            raise RuntimeError("Book has wrong type.")
        progress = await self._client.get_my_media_progress(item_id=prov_audiobook_id)
        return parse_audiobook(
            abs_audiobook=abs_audiobook,
            lookup_key=self.lookup_key,
            domain=self.domain,
            instance_id=self.instance_id,
            token=self._client.token,
            base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
            media_progress=progress,
        )

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Get stream of item."""
        # self.logger.debug(f"Streamdetails: {item_id}")
        if media_type == MediaType.PODCAST_EPISODE:
            return await self._get_stream_details_podcast_episode(item_id)
        elif media_type == MediaType.AUDIOBOOK:
            abs_audiobook = await self._client.get_library_item_book(book_id=item_id, expanded=True)
            if type(abs_audiobook) is not AbsLibraryItemExpandedBook:
                raise RuntimeError("Book has wrong type.")
            tracks = abs_audiobook.media.tracks
            if len(tracks) == 0:
                raise MediaNotFoundError("Stream not found")
            if len(tracks) > 1:
                # Adding audiobook id to device id makes multiple sessions for different books
                # possible, but we can an already opened session for a particular book, if
                # it exists.
                _device_info = deepcopy(self.device_info)
                _device_info.device_id += f"/{item_id}"
                self.logger.debug(
                    "Using playback via a session for audiobook "
                    f'"{abs_audiobook.media.metadata.title}"'
                )
                session = await self._client.get_playback_session_music_assistant(
                    device_info=_device_info, book_id=item_id
                )
                # small delay, allow abs to launch ffmpeg process
                await asyncio.sleep(1)
                return await self._get_streamdetails_from_playback_session(session)
            self.logger.debug(
                f'Using direct playback for audiobook "{abs_audiobook.media.metadata.title}".'
            )
            return await self._get_stream_details_audiobook(abs_audiobook)
        raise MediaNotFoundError("Stream unknown")

    async def _get_streamdetails_from_playback_session(
        self, session: AbsPlaybackSessionExpanded
    ) -> StreamDetails:
        """Give Streamdetails from given session."""
        tracks = session.audio_tracks
        if len(tracks) == 0:
            raise RuntimeError("Playback session has no tracks to play")
        track = tracks[0]
        track_url = track.content_url
        if track_url.split("/")[1] != "hls":
            raise RuntimeError("Did expect HLS stream for session playback")
        item_id = ""
        media_type = MediaType.AUDIOBOOK
        audiobook_id = session.library_item_id
        self.session_ids.add(session.id_)
        item_id = f"{audiobook_id} {session.id_}"
        token = self._client.token
        base_url = str(self.config.get_value(CONF_URL))
        media_url = track.content_url
        stream_url = f"{base_url}{media_url}?token={token}"
        self.logger.debug(f"Using session with id {session.id_}.")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,
            ),
            media_type=media_type,
            stream_type=StreamType.HLS,
            path=stream_url,
        )

    async def _get_stream_details_audiobook(
        self, abs_audiobook: AbsLibraryItemExpandedBook
    ) -> StreamDetails:
        """Only single audio file in audiobook."""
        tracks = abs_audiobook.media.tracks
        token = self._client.token
        base_url = str(self.config.get_value(CONF_URL))
        media_url = tracks[0].content_url
        stream_url = f"{base_url}{media_url}?token={token}"
        # audiobookshelf returns information of stream, so we should be able
        # to lift unknown at some point.
        return StreamDetails(
            provider=self.lookup_key,
            item_id=abs_audiobook.id_,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,
            ),
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.HTTP,
            path=stream_url,
        )

    async def _get_stream_details_podcast_episode(self, podcast_id: str) -> StreamDetails:
        """Stream of a Podcast."""
        abs_podcast_id, abs_episode_id = podcast_id.split(" ")
        abs_episode = None

        abs_podcast = await self._client.get_library_item_podcast(
            podcast_id=abs_podcast_id, expanded=True
        )
        if type(abs_podcast) is not AbsLibraryItemExpandedPodcast:
            raise RuntimeError("Podcast has wrong type.")
        for abs_episode in abs_podcast.media.episodes:
            if abs_episode.id_ == abs_episode_id:
                break
        if abs_episode is None:
            raise MediaNotFoundError("Stream not found")
        self.logger.debug(f'Using direct playback for podcast episode "{abs_episode.title}".')
        token = self._client.token
        base_url = str(self.config.get_value(CONF_URL))
        media_url = abs_episode.audio_track.content_url
        full_url = f"{base_url}{media_url}?token={token}"
        return StreamDetails(
            provider=self.lookup_key,
            item_id=podcast_id,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=full_url,
        )

    async def on_played(
        self, media_type: MediaType, item_id: str, fully_played: bool, position: int
    ) -> None:
        """Update progress in Audiobookshelf.

        In our case media_type may have 3 values:
            - PODCAST
            - PODCAST_EPISODE
            - AUDIOBOOK
        We ignore PODCAST (function is called on adding a podcast with position=None)

        """
        # self.logger.debug(f"on_played: {media_type=} {item_id=}, {fully_played=} {position=}")
        if media_type == MediaType.PODCAST_EPISODE:
            abs_podcast_id, abs_episode_id = item_id.split(" ")
            mass_podcast_episode = await self.get_podcast_episode(item_id)
            duration = mass_podcast_episode.duration
            self.logger.debug(
                f"Updating media progress of {media_type.value}, title {mass_podcast_episode.name}."
            )
            await self._client.update_my_media_progress(
                item_id=abs_podcast_id,
                episode_id=abs_episode_id,
                duration_seconds=duration,
                progress_seconds=position,
                is_finished=fully_played,
            )
        if media_type == MediaType.AUDIOBOOK:
            mass_audiobook = await self.get_audiobook(item_id)
            duration = mass_audiobook.duration
            self.logger.debug(f"Updating {media_type.value} named {mass_audiobook.name} progress")
            await self._client.update_my_media_progress(
                item_id=item_id,
                duration_seconds=duration,
                progress_seconds=position,
                is_finished=fully_played,
            )

    async def _close_all_playback_sessions(self) -> None:
        for session_id in self.session_ids:
            try:
                await self._client.close_open_session(session_id=session_id)
            except AbsApiError:
                self.logger.warning("Was unable to close playback session %s", session_id)
