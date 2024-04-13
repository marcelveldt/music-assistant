"""Built-in/generic provider to handle media from files and (remote) urls."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING, NotRequired, TypedDict

from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant.common.models.errors import MediaNotFoundError
from music_assistant.common.models.media_items import (
    AlbumTrack,
    Artist,
    AudioFormat,
    MediaItemImage,
    MediaItemType,
    Playlist,
    PlaylistTrack,
    ProviderMapping,
    Radio,
    Track,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.server.helpers.tags import AudioTags, parse_tags
from music_assistant.server.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


class StoredItem(TypedDict):
    """Definition of an media item (for the builtin provider) stored in persistent storage."""

    path: str  # url or (locally accessible) file path
    name: str
    image_url: NotRequired[str]
    items: NotRequired[list[str]]  # playlists only


CONF_KEY_RADIOS = "stored_radios"
CONF_KEY_TRACKS = "stored_tracks"
CONF_KEY_PLAYLISTS = "stored_playlists"


ALL_LIBRARY_TRACKS = "all_library_tracks"
ALL_FAVORITE_TRACKS = "all_favorite_tracks"
RANDOM_ARTIST = "random_artist"
RANDOM_ALBUM = "random_album"
RANDOM_TRACKS = "random_tracks"

BUILTIN_PLAYLISTS = {
    ALL_LIBRARY_TRACKS: "All library tracks",
    ALL_FAVORITE_TRACKS: "All favorited tracks",
    RANDOM_ARTIST: "Random Artist (from library)",
    RANDOM_ALBUM: "Random Album (from library)",
    RANDOM_TRACKS: "100 Random tracks (from library)",
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return BuiltinProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return tuple(
        ConfigEntry(
            key=key,
            type=ConfigEntryType.BOOLEAN,
            label=name,
            default_value=True,
            category="builtin_playlists",
        )
        for key, name in BUILTIN_PLAYLISTS.items()
    )


class BuiltinProvider(MusicProvider):
    """Built-in/generic provider to handle (manually added) media from files and (remote) urls."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_RADIOS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.LIBRARY_TRACKS_EDIT,
            ProviderFeature.LIBRARY_RADIOS_EDIT,
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        # always prefer db item for existing items
        if db_item := await self.mass.music.tracks.get_library_item_by_prov_id(
            prov_track_id, self.instance_id
        ):
            return db_item
        # fallback to parsing (assuming the id is an url or local file path)
        parsed_item = await self.parse_item(prov_track_id)
        stored_item: StoredItem
        if stored_item := self.mass.config.get(f"{CONF_KEY_TRACKS}/{parsed_item.item_id}"):
            # always prefer the stored info, such as the name
            parsed_item.name = stored_item["name"]
            if image_url := stored_item.get("image_url"):
                parsed_item.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
        return parsed_item

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        # always prefer db item for existing items
        if db_item := await self.mass.music.radio.get_library_item_by_prov_id(
            prov_radio_id, self.instance_id
        ):
            return db_item
        # fallback to parsing (assuming the id is an url or local file path)
        parsed_item = await self.parse_item(prov_radio_id, force_radio=True)
        stored_item: StoredItem
        if stored_item := self.mass.config.get(f"{CONF_KEY_RADIOS}/{parsed_item.item_id}"):
            # always prefer the stored info, such as the name
            parsed_item.name = stored_item["name"]
            if image_url := stored_item.get("image_url"):
                parsed_item.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
        return parsed_item

    async def get_artist(self, prov_artist_id: str) -> Track:
        """Get full artist details by id."""
        artist = prov_artist_id
        # this is here for compatibility reasons only
        return Artist(
            item_id=artist,
            provider=self.domain,
            name=artist,
            provider_mappings={
                ProviderMapping(
                    item_id=artist,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=False,
                )
            },
        )

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if prov_playlist_id in BUILTIN_PLAYLISTS:
            # this is one of our builtin/default playlists
            return Playlist(
                item_id=prov_playlist_id,
                provider=self.instance_id,
                name=BUILTIN_PLAYLISTS[prov_playlist_id],
                provider_mappings={
                    ProviderMapping(
                        item_id=prov_playlist_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
                owner="Music Assistant",
                is_editable=False,
            )
        # user created universal playlist
        # always prefer db item for existing items
        if db_item := await self.mass.music.tracks.get_library_item_by_prov_id(
            prov_playlist_id, self.instance_id
        ):
            return db_item
        stored_item: StoredItem = self.mass.config.get(f"{CONF_KEY_PLAYLISTS}/{prov_playlist_id}")
        if not stored_item:
            raise MediaNotFoundError
        playlist = Playlist(
            item_id=prov_playlist_id,
            provider=self.instance_id,
            name=stored_item["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=prov_playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            owner="Music Assistant",
            is_editable=True,
        )
        if image_url := stored_item.get("image_url"):
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        return playlist

    async def get_item(self, media_type: MediaType, prov_item_id: str) -> MediaItemType:
        """Get single MediaItem from provider."""
        if media_type == MediaType.ARTIST:
            return await self.get_artist(prov_item_id)
        if media_type == MediaType.TRACK:
            return await self.get_track(prov_item_id)
        if media_type == MediaType.RADIO:
            return await self.get_radio(prov_item_id)
        if media_type == MediaType.PLAYLIST:
            return await self.get_playlist(prov_item_id)
        if media_type == MediaType.UNKNOWN:
            return await self.parse_item(prov_item_id)
        raise NotImplementedError

    async def get_library_tracks(self) -> AsyncGenerator[Track | AlbumTrack, None]:
        """Retrieve library tracks from the provider."""
        stored_items: dict[str, StoredItem] = self.mass.config.get(f"{CONF_KEY_TRACKS}", {})
        for item_id in stored_items:
            yield await self.get_track(item_id)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library/subscribed playlists from the provider."""
        # return user stored playlists
        stored_items: dict[str, StoredItem] = self.mass.config.get(f"{CONF_KEY_RADIOS}", {})
        for item_id in stored_items:
            yield await self.get_playlist(item_id)
        # return builtin playlists
        for item_id in BUILTIN_PLAYLISTS:
            if self.config.get_value(item_id) is False:
                continue
            yield await self.get_playlist(item_id)

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        stored_items: dict[str, StoredItem] = self.mass.config.get(f"{CONF_KEY_RADIOS}", {})
        for item_id in stored_items:
            yield await self.get_radio(item_id)

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        if item.media_type == MediaType.TRACK:
            key = f"{CONF_KEY_TRACKS}/{item.item_id}"
        elif item.media_type == MediaType.RADIO:
            key = f"{CONF_KEY_RADIOS}/{item.item_id}"
        else:
            return False
        stored_item = StoredItem(path=item.item_id, name=item.name)
        if item.image:
            stored_item["image_url"] = item.image
        self.mass.config.set(key, stored_item)
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        if media_type == MediaType.TRACK:
            # regular manual track URL/path
            key = f"{CONF_KEY_TRACKS}/{prov_item_id}"
            self.mass.config.remove(key, key)
        elif media_type == MediaType.RADIO:
            # regular manual radio URL/path
            key = f"{CONF_KEY_PLAYLISTS}/{prov_item_id}"
            self.mass.config.remove(key, key)
        elif media_type == MediaType.PLAYLIST and prov_item_id in BUILTIN_PLAYLISTS:
            # user wants to disable/remove one of our builtin playlists
            # to prevent it comes back, we mark it as disabled in config
            await self.mass.config.set_provider_config_value(self.instance_id, prov_item_id, False)
        elif media_type == MediaType.PLAYLIST:
            # manually added (multi provider) playlist removal
            key = f"{CONF_KEY_PLAYLISTS}/{prov_item_id}"
            self.mass.config.remove(key, key)
        else:
            return False
        return True

    async def get_playlist_tracks(
        self, prov_playlist_id: str
    ) -> AsyncGenerator[PlaylistTrack, None]:
        # handle built-in playlists
        """Get all playlist tracks for given playlist id."""
        if prov_playlist_id in BUILTIN_PLAYLISTS:
            async for item in self._get_builtin_playlist_tracks(prov_playlist_id):
                yield item
            return
        # user created universal playlist
        stored_item: StoredItem = self.mass.config.get(f"{CONF_KEY_PLAYLISTS}/{prov_playlist_id}")
        if not stored_item:
            raise MediaNotFoundError
        playlist_items = stored_item.get("items", [])
        for count, playlist_item_uri in enumerate(playlist_items, 1):
            with suppress(MediaNotFoundError):
                base_item = await self.mass.music.get_item_by_uri(playlist_item_uri)
                yield PlaylistTrack.from_dict({**base_item.to_dict(), "position": count})

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        if ProviderFeature.PLAYLIST_TRACKS_EDIT in self.supported_features:
            raise NotImplementedError

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        if ProviderFeature.PLAYLIST_TRACKS_EDIT in self.supported_features:
            raise NotImplementedError

    async def create_playlist(self, name: str) -> Playlist:  # type: ignore[return]
        """Create a new playlist on provider with given name."""
        if ProviderFeature.PLAYLIST_CREATE in self.supported_features:
            raise NotImplementedError

    async def parse_item(
        self,
        url: str,
        force_refresh: bool = False,
        force_radio: bool = False,
    ) -> Track | Radio:
        """Parse plain URL to MediaItem of type Radio or Track."""
        media_info = await self._get_media_info(url, force_refresh)
        is_radio = media_info.get("icy-name") or not media_info.duration
        provider_mappings = {
            ProviderMapping(
                item_id=url,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.try_parse(media_info.format),
                    sample_rate=media_info.sample_rate,
                    bit_depth=media_info.bits_per_sample,
                    bit_rate=media_info.bit_rate,
                ),
            )
        }
        if is_radio or force_radio:
            # treat as radio
            media_item = Radio(
                item_id=url,
                provider=self.domain,
                name=media_info.get("icy-name") or url,
                provider_mappings=provider_mappings,
            )
        else:
            media_item = Track(
                item_id=url,
                provider=self.domain,
                name=media_info.title or url,
                duration=int(media_info.duration or 0),
                artists=[await self.get_artist(artist) for artist in media_info.artists],
                provider_mappings=provider_mappings,
            )

        if media_info.has_cover_image:
            media_item.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=url,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            ]
        return media_item

    async def resolve_image(self, path: str) -> str | bytes | AsyncGenerator[bytes, None]:
        """Resolve the image."""
        return path

    async def _get_media_info(self, url: str, force_refresh: bool = False) -> AudioTags:
        """Retrieve mediainfo for url."""
        # do we have some cached info for this url ?
        cache_key = f"{self.instance_id}.media_info.{url}"
        cached_info = await self.mass.cache.get(cache_key)
        if cached_info and not force_refresh:
            return AudioTags.parse(cached_info)
        # parse info with ffprobe (and store in cache)
        media_info = await parse_tags(url)
        if "authSig" in url:
            media_info.has_cover_image = False
        await self.mass.cache.set(cache_key, media_info.raw)
        return media_info

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        media_info = await self._get_media_info(item_id)
        is_radio = media_info.get("icy-name") or not media_info.duration
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(media_info.format),
                sample_rate=media_info.sample_rate,
                bit_depth=media_info.bits_per_sample,
                channels=media_info.channels,
            ),
            media_type=MediaType.RADIO if is_radio else MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=item_id,
            can_seek=not is_radio,
        )

    async def _get_builtin_playlist_tracks(
        self, builtin_playlist_id: str
    ) -> AsyncGenerator[PlaylistTrack, None]:
        """Get all playlist tracks for given builtin playlist id."""
        count = 0
        if builtin_playlist_id == ALL_LIBRARY_TRACKS:
            async for item in self.mass.music.tracks.iter_library_items(order_by="RANDOM()"):
                count += 1
                yield PlaylistTrack.from_dict({**item.to_dict(), "position": count})
            return
        if builtin_playlist_id == ALL_FAVORITE_TRACKS:
            async for item in self.mass.music.tracks.iter_library_items(
                favorite=True, order_by="RANDOM()"
            ):
                count += 1
                yield PlaylistTrack.from_dict({**item.to_dict(), "position": count})
            return
        if builtin_playlist_id == RANDOM_TRACKS:
            async for item in self.mass.music.tracks.iter_library_items(order_by="RANDOM()"):
                count += 1
                yield PlaylistTrack.from_dict({**item.to_dict(), "position": count})
                if count == 100:
                    break
            return
        if builtin_playlist_id == RANDOM_ALBUM:
            async for random_album in self.mass.music.albums.iter_library_items(
                order_by="RANDOM()"
            ):
                for album_track in await self.mass.music.albums.tracks(
                    random_album.item_id, random_album.provider
                ):
                    count += 1
                    yield PlaylistTrack.from_dict({**album_track.to_dict(), "position": count})
                if count > 0:
                    return
        if builtin_playlist_id == RANDOM_ARTIST:
            async for random_album in self.mass.music.artists.iter_library_items(
                order_by="RANDOM()"
            ):
                for artist_track in await self.mass.music.artists.tracks(
                    random_album.item_id, random_album.provider
                ):
                    count += 1
                    yield PlaylistTrack.from_dict({**artist_track.to_dict(), "position": count})
                if count > 0:
                    return
