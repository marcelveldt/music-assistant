"""Audiobookshelf provider for Music Assistant.

Audiobookshelf is abbreviated ABS here.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.audiobookshelf.abs_client import (
    ABSClient,
)
from music_assistant.providers.audiobookshelf.abs_schema import (
    ABSAudioBook,
    ABSLibrary,
    ABSPodcast,
    ABSPodcastEpisodeExpanded,
)

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"


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
        self._client = ABSClient()
        await self._client.init(
            session=self.mass.http_session,
            base_url=str(self.config.get_value(CONF_URL)),
            username=str(self.config.get_value(CONF_USERNAME)),
            password=str(self.config.get_value(CONF_PASSWORD)),
            check_ssl=bool(self.config.get_value(CONF_VERIFY_SSL)),
        )
        await self._client.sync()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        await self._client.logout()

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        return False

    async def sync_library(self, media_types: tuple[MediaType, ...]) -> None:
        """Run library sync for this provider."""
        await self._client.sync()
        await super().sync_library(media_types=media_types)

    def _parse_podcast(self, abs_podcast: ABSPodcast) -> Podcast:
        """Translate ABSPodcast to MassPodcast."""
        title = abs_podcast.media.metadata.title
        # Per API doc title may be None.
        if title is None:
            title = "UNKNOWN"
        mass_podcast = Podcast(
            item_id=abs_podcast.id_,
            name=title,
            publisher=abs_podcast.media.metadata.author,
            provider=self.domain,
            total_episodes=abs_podcast.media.num_episodes,
            provider_mappings={
                ProviderMapping(
                    item_id=abs_podcast.id_,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        mass_podcast.metadata.description = abs_podcast.media.metadata.description
        token = self._client.token
        image_url = f"{self.config.get_value(CONF_URL)}/api/items/{abs_podcast.id_}/cover?token={token}"
        mass_podcast.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=image_url, provider=self.lookup_key)]
        )
        mass_podcast.metadata.explicit = abs_podcast.media.metadata.explicit
        if abs_podcast.media.metadata.language is not None:
            mass_podcast.metadata.languages = UniqueList([abs_podcast.media.metadata.language])
        if abs_podcast.media.metadata.genres is not None:
            mass_podcast.metadata.genres = set(abs_podcast.media.metadata.genres)
        mass_podcast.metadata.release_date = abs_podcast.media.metadata.release_date

        return mass_podcast

    async def _parse_podcast_episode(
        self,
        episode: ABSPodcastEpisodeExpanded,
        prov_podcast_id: str,
        fallback_episode_cnt: int | None = None,
    ) -> PodcastEpisode:
        """Translate ABSPodcastEpisode to MassPodcastEpisode.

        For an episode the id is set to f"{podcast_id} {episode_id}".
        ABS ids have no spaces, so we can split at a space to retrieve both
        in other functions.
        """
        url = f"{self.config.get_value(CONF_URL)}{episode.audio_track.content_url}"
        episode_id = f"{prov_podcast_id} {episode.id_}"

        if episode.published_at is not None:
            position = -episode.published_at
        else:
            position = 0
            if fallback_episode_cnt is not None:
                position = fallback_episode_cnt
        mass_episode = PodcastEpisode(
            item_id=episode_id,
            provider=self.domain,
            name=episode.title,
            duration=int(episode.duration),
            position=position,
            podcast=ItemMapping(
                item_id=prov_podcast_id,
                provider=self.instance_id,
                name=episode.title,
                media_type=MediaType.PODCAST,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=episode_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.UNKNOWN,
                    ),
                    url=url,
                )
            },
        )
        progress, finished = await self._client.get_podcast_progress_ms(
            prov_podcast_id, episode.id_
        )
        if progress is not None:
            mass_episode.resume_position_ms = progress
            mass_episode.fully_played = finished

        # cover image
        url_base = f"{self.config.get_value(CONF_URL)}"
        url_api = f"/api/items/{prov_podcast_id}/cover?token={self._client.token}"
        url_cover = f"{url_base}{url_api}"
        mass_episode.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=url_cover, provider=self.lookup_key)]
        )

        return mass_episode

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library/subscribed podcasts from the provider."""
        async for abs_podcast in self._client.get_all_podcasts():
            mass_podcast = self._parse_podcast(abs_podcast)
            yield mass_podcast

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get single podcast."""
        abs_podcast = await self._client.get_podcast(prov_podcast_id)
        return self._parse_podcast(abs_podcast)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> list[PodcastEpisode]:
        """Get all podcast episodes of podcast."""
        abs_podcast = await self._client.get_podcast(prov_podcast_id)
        episode_list = []
        episode_cnt = 1
        for abs_episode in abs_podcast.media.episodes:
            mass_episode = await self._parse_podcast_episode(
                abs_episode, prov_podcast_id, episode_cnt
            )
            episode_list.append(mass_episode)
            episode_cnt += 1
        return episode_list

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get single podcast episode."""
        prov_podcast_id, e_id = prov_episode_id.split(" ")
        abs_podcast = await self._client.get_podcast(prov_podcast_id)
        episode_cnt = 1
        for abs_episode in abs_podcast.media.episodes:
            if abs_episode.id_ == e_id:
                return await self._parse_podcast_episode(abs_episode, prov_podcast_id, episode_cnt)

            episode_cnt += 1
        raise MediaNotFoundError("Episode not found")

    async def _parse_audiobook(self, abs_audiobook: ABSAudioBook) -> Audiobook:
        mass_audiobook = Audiobook(
            item_id=abs_audiobook.id_,
            provider=self.domain,
            name=abs_audiobook.media.metadata.title,
            duration=int(abs_audiobook.media.duration),
            provider_mappings={
                ProviderMapping(
                    item_id=abs_audiobook.id_,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            publisher=abs_audiobook.media.metadata.publisher,
            authors=UniqueList([x.name for x in abs_audiobook.media.metadata.authors]),
            narrators=UniqueList(abs_audiobook.media.metadata.narrators),
        )
        mass_audiobook.metadata.description = abs_audiobook.media.metadata.description
        if abs_audiobook.media.metadata.language is not None:
            mass_audiobook.metadata.languages = UniqueList([abs_audiobook.media.metadata.language])
        mass_audiobook.metadata.release_date = abs_audiobook.media.metadata.published_date
        if abs_audiobook.media.metadata.genres is not None:
            mass_audiobook.metadata.genres = set(abs_audiobook.media.metadata.genres)

        # chapters
        chapters = []
        for idx, chapter in enumerate(abs_audiobook.media.chapters):
            chapters.append(
                MediaItemChapter(
                    position=idx + 1,  # chapter starting at 1
                    name=chapter.title,
                    start=chapter.start,
                    end=chapter.end,
                )
            )
        mass_audiobook.metadata.chapters = chapters

        mass_audiobook.metadata.explicit = abs_audiobook.media.metadata.explicit
        progress, finished = await self._client.get_audiobook_progress_ms(abs_audiobook.id_)
        if progress is not None:
            mass_audiobook.resume_position_ms = progress
            mass_audiobook.fully_played = finished

        # cover
        base_url = f"{self.config.get_value(CONF_URL)}"
        api_url = f"/api/items/{abs_audiobook.id_}/cover?token={self._client.token}"
        cover_url = f"{base_url}{api_url}"
        mass_audiobook.metadata.images = UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=self.lookup_key)]
        )

        return mass_audiobook

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
        """Get Audiobook libraries."""
        async for abs_audiobook in self._client.get_all_audiobooks():
            mass_audiobook = await self._parse_audiobook(abs_audiobook)
            yield mass_audiobook

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get a single audiobook."""
        abs_audiobook = await self._client.get_audiobook(prov_audiobook_id)
        return await self._parse_audiobook(abs_audiobook)

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Get stream of item."""
        if media_type == MediaType.PODCAST_EPISODE:
            return await self._get_stream_details_podcast_episode(item_id)
        elif media_type == MediaType.AUDIOBOOK:
            return await self._get_stream_details_audiobook(item_id)
        raise MediaNotFoundError("Stream unknown")

    async def _get_stream_details_audiobook(self, audiobook_id: str) -> StreamDetails:
        """Only single audio file in audiobook."""
        abs_audiobook = await self._client.get_audiobook(audiobook_id)
        tracks = abs_audiobook.media.tracks
        if len(tracks) == 0:
            raise MediaNotFoundError("Stream not found")
        if len(tracks) > 1:
            logging.warning("Music Assistant only supports single file base audiobooks")
        token = self._client.token
        base_url = str(self.config.get_value(CONF_URL))
        media_url = tracks[0].content_url
        stream_url = f"{base_url}{media_url}?token={token}"
        # audiobookshelf returns information of stream, so we should be able
        # to lift unknown at some point.
        return StreamDetails(
            provider=self.instance_id,
            item_id=audiobook_id,
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

        abs_podcast = await self._client.get_podcast(abs_podcast_id)
        for abs_episode in abs_podcast.media.episodes:
            if abs_episode.id_ == abs_episode_id:
                break
        if abs_episode is None:
            raise MediaNotFoundError("Stream not found")
        token = self._client.token
        base_url = str(self.config.get_value(CONF_URL))
        media_url = abs_episode.audio_track.content_url
        full_url = f"{base_url}{media_url}?token={token}"
        return StreamDetails(
            provider=self.instance_id,
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
        """Update progress in Audiobookshelf."""
        if media_type == MediaType.PODCAST_EPISODE:
            abs_podcast_id, abs_episode_id = item_id.split(" ")
            mass_podcast_episode = await self.get_podcast_episode(item_id)
            duration = mass_podcast_episode.duration
            await self._client.update_podcast_progress(
                podcast_id=abs_podcast_id,
                episode_id=abs_episode_id,
                progress_s=position,
                duration_s=duration,
                is_finished=fully_played,
            )
        if media_type == MediaType.AUDIOBOOK:
            mass_audiobook = await self.get_audiobook(item_id)
            duration = mass_audiobook.duration
            await self._client.update_audiobook_progress(
                audiobook_id=item_id,
                progress_s=position,
                duration_s=duration,
                is_finished=fully_played,
            )

    async def _browse_root(
        self, library_list: list[ABSLibrary], item_path: str
    ) -> Sequence[MediaItemType | ItemMapping]:
        """Browse root folder in browse view.

        Helper functions. Shows the library name, ABS supports multiple libraries
        of both podcasts and audiobooks.
        """
        items: list[MediaItemType | ItemMapping] = []
        for library in library_list:
            items.append(
                BrowseFolder(
                    item_id=library.id_,
                    name=library.name,
                    provider=self.instance_id,
                    path=f"{self.instance_id}://{item_path}/{library.id_}",
                )
            )
        return items

    async def _browse_lib(
        self,
        library_id: str,
        library_list: list[ABSLibrary],
        media_type: MediaType,
    ) -> Sequence[MediaItemType | ItemMapping]:
        """Browse lib folder in browse view.

        Helper functions. Shows the items which are part of an ABS library.
        """
        library = None
        for library in library_list:
            if library_id == library.id_:
                break
        if library is None:
            raise MediaNotFoundError("Lib missing.")

        def get_item_mapping(item: ABSAudioBook | ABSPodcast) -> ItemMapping:
            title = item.media.metadata.title
            if title is None:
                title = "UNKNOWN"
            token = self._client.token
            url = f"{self.config.get_value(CONF_URL)}/api/items/{item.id_}/cover?token={token}"
            image = MediaItemImage(type=ImageType.THUMB, path=url, provider=self.lookup_key)
            return ItemMapping(
                media_type=media_type,
                item_id=item.id_,
                provider=self.instance_id,
                name=title,
                image=image,
            )

        items: list[MediaItemType | ItemMapping] = []
        if media_type == MediaType.PODCAST:
            async for podcast in self._client.get_all_podcasts_by_library(library):
                items.append(get_item_mapping(podcast))
        elif media_type == MediaType.AUDIOBOOK:
            async for audiobook in self._client.get_all_audiobooks_by_library(library):
                items.append(get_item_mapping(audiobook))
        else:
            raise RuntimeError(f"Media type must not be {media_type}")
        return items

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping]:
        """Browse features shows libraries names."""
        item_path = path.split("://", 1)[1]
        if not item_path:  # root
            return await super().browse(path)

        # HANDLE ROOT PATH
        if item_path == "audiobooks":
            library_list = self._client.audiobook_libraries
            return await self._browse_root(library_list, item_path)
        elif item_path == "podcasts":
            library_list = self._client.podcast_libraries
            return await self._browse_root(library_list, item_path)

        # HANDLE WITHIN LIBRARY
        library_type, library_id = item_path.split("/")
        if library_type == "audiobooks":
            library_list = self._client.audiobook_libraries
            media_type = MediaType.AUDIOBOOK
        elif library_type == "podcasts":
            library_list = self._client.podcast_libraries
            media_type = MediaType.PODCAST
        else:
            raise MediaNotFoundError("Specified Lib Type unknown")

        return await self._browse_lib(library_id, library_list, media_type)
