"""Jellyfin support for MusicAssistant."""

from __future__ import annotations

import logging
import mimetypes
import socket
import uuid
from asyncio import TaskGroup
from collections.abc import AsyncGenerator

from aiojellyfin import MediaItem as JellyMediaItem
from aiojellyfin import MediaLibrary as JellyMediaLibrary
from aiojellyfin import SessionConfiguration, authenticate_by_name

from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant.common.models.errors import InvalidDataError, LoginFailed, MediaNotFoundError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.server.models import ProviderInstanceType
from music_assistant.server.models.music_provider import MusicProvider
from music_assistant.server.server import MusicAssistant

from .const import (
    ALBUM_FIELDS,
    ARTIST_FIELDS,
    CLIENT_VERSION,
    ITEM_KEY_ALBUM,
    ITEM_KEY_ALBUM_ARTIST,
    ITEM_KEY_ALBUM_ARTISTS,
    ITEM_KEY_ALBUM_ID,
    ITEM_KEY_ARTIST_ITEMS,
    ITEM_KEY_CAN_DOWNLOAD,
    ITEM_KEY_COLLECTION_TYPE,
    ITEM_KEY_ID,
    ITEM_KEY_IMAGE_TAGS,
    ITEM_KEY_MEDIA_CHANNELS,
    ITEM_KEY_MEDIA_CODEC,
    ITEM_KEY_MEDIA_SOURCES,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_MUSICBRAINZ_ARTIST,
    ITEM_KEY_MUSICBRAINZ_RELEASE_GROUP,
    ITEM_KEY_MUSICBRAINZ_TRACK,
    ITEM_KEY_NAME,
    ITEM_KEY_OVERVIEW,
    ITEM_KEY_PARENT_ID,
    ITEM_KEY_PARENT_INDEX_NUM,
    ITEM_KEY_PRODUCTION_YEAR,
    ITEM_KEY_PROVIDER_IDS,
    ITEM_KEY_RUNTIME_TICKS,
    ITEM_KEY_SORT_NAME,
    ITEM_KEY_USER_DATA,
    ITEM_TYPE_ALBUM,
    ITEM_TYPE_ARTIST,
    ITEM_TYPE_AUDIO,
    ITEM_TYPE_MUSICARTISTS,
    MAX_IMAGE_WIDTH,
    SUPPORTED_CONTAINER_FORMATS,
    TRACK_FIELDS,
    USER_APP_NAME,
    USER_DATA_KEY_IS_FAVORITE,
)

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
FAKE_ARTIST_PREFIX = "_fake://"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = JellyfinProvider(mass, manifest, config)
    await prov.handle_async_init()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # pylint: disable=W0613
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # config flow auth action/step (authenticate button clicked)
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            label="Server",
            required=True,
            description="The url of the Jellyfin server to connect to.",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
            description="The username to authenticate to the remote server."
            "the remote host, For example 'media'.",
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


class JellyfinProvider(MusicProvider):
    """Provider for a jellyfin music library."""

    async def handle_async_init(self) -> None:
        """Initialize provider(instance) with given configuration."""
        session_config = SessionConfiguration(
            session=self.mass.http_session,
            url=str(self.config.get_value(CONF_URL)),
            verify_ssl=bool(self.config.get_value(CONF_VERIFY_SSL)),
            app_name=USER_APP_NAME,
            app_version=CLIENT_VERSION,
            device_name=socket.gethostname(),
            device_id=str(uuid.uuid4()),
        )

        try:
            self._client = await authenticate_by_name(
                session_config,
                str(self.config.get_value(CONF_USERNAME)),
                str(self.config.get_value(CONF_PASSWORD)),
            )
        except Exception as err:
            raise LoginFailed(f"Authentication failed: {err}") from err

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return a list of supported features."""
        return (
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.ARTIST_ALBUMS,
        )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    async def _search_track(self, search_query: str, limit: int) -> list[Track]:
        resultset = await self._client.search_media_items(
            term=search_query,
            media=ITEM_TYPE_AUDIO,
            limit=limit,
            enable_user_data=True,
            fields=TRACK_FIELDS,
        )
        tracks = []
        for item in resultset["Items"]:
            tracks.append(await self._parse_track(item))
        return tracks

    async def _search_album(self, search_query: str, limit: int) -> list[Album]:
        if "-" in search_query:
            searchterms = search_query.split(" - ")
            albumname = searchterms[1]
        else:
            albumname = search_query
        resultset = await self._client.search_media_items(
            term=albumname,
            media=ITEM_TYPE_ALBUM,
            limit=limit,
            enable_user_data=True,
            fields=ALBUM_FIELDS,
        )
        albums = []
        for item in resultset["Items"]:
            albums.append(self._parse_album(item))
        return albums

    async def _search_artist(self, search_query: str, limit: int) -> list[Artist]:
        resultset = await self._client.search_media_items(
            term=search_query,
            media=ITEM_TYPE_ARTIST,
            limit=limit,
            enable_user_data=True,
            fields=ARTIST_FIELDS,
        )
        artists = []
        for item in resultset["Items"]:
            artists.append(self._parse_artist(item))
        return artists

    async def _search_playlist(self, search_query: str, limit: int) -> list[Playlist]:
        resultset = await self._client.search_media_items(
            term=search_query,
            media="Playlist",
            limit=limit,
        )
        playlists = []
        for item in resultset["Items"]:
            playlists.append(self._parse_playlist(item))
        return playlists

    def _parse_album(self, jellyfin_album: JellyMediaItem) -> Album:
        """Parse a Jellyfin Album response to an Album model object."""
        album_id = jellyfin_album[ITEM_KEY_ID]
        album = Album(
            item_id=album_id,
            provider=self.domain,
            name=jellyfin_album[ITEM_KEY_NAME],
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if ITEM_KEY_PRODUCTION_YEAR in jellyfin_album:
            album.year = jellyfin_album[ITEM_KEY_PRODUCTION_YEAR]
        if thumb := self._get_thumbnail_url(jellyfin_album):
            album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb,
                        provider=self.instance_id,
                        remotely_accessible=False,
                    )
                ]
            )
        if ITEM_KEY_OVERVIEW in jellyfin_album:
            album.metadata.description = jellyfin_album[ITEM_KEY_OVERVIEW]
        if ITEM_KEY_MUSICBRAINZ_RELEASE_GROUP in jellyfin_album[ITEM_KEY_PROVIDER_IDS]:
            try:
                album.mbid = jellyfin_album[ITEM_KEY_PROVIDER_IDS][
                    ITEM_KEY_MUSICBRAINZ_RELEASE_GROUP
                ]
            except InvalidDataError as error:
                self.logger.warning(
                    "Jellyfin has an invalid musicbrainz id for album %s",
                    album.name,
                    exc_info=error if self.logger.isEnabledFor(logging.DEBUG) else None,
                )
        if ITEM_KEY_SORT_NAME in jellyfin_album:
            album.sort_name = jellyfin_album[ITEM_KEY_SORT_NAME]
        if ITEM_KEY_ALBUM_ARTIST in jellyfin_album:
            for album_artist in jellyfin_album[ITEM_KEY_ALBUM_ARTISTS]:
                album.artists.append(
                    self._get_item_mapping(
                        MediaType.ARTIST,
                        album_artist[ITEM_KEY_ID],
                        album_artist[ITEM_KEY_NAME],
                    )
                )
        elif len(jellyfin_album.get(ITEM_KEY_ARTIST_ITEMS, [])) >= 1:
            for artist_item in jellyfin_album[ITEM_KEY_ARTIST_ITEMS]:
                album.artists.append(
                    self._get_item_mapping(
                        MediaType.ARTIST,
                        artist_item[ITEM_KEY_ID],
                        artist_item[ITEM_KEY_NAME],
                    )
                )
        user_data = jellyfin_album.get(ITEM_KEY_USER_DATA, {})
        album.favorite = user_data.get(USER_DATA_KEY_IS_FAVORITE, False)
        return album

    def _parse_artist(self, jellyfin_artist: JellyMediaItem) -> Artist:
        """Parse a Jellyfin Artist response to Artist model object."""
        artist_id = jellyfin_artist[ITEM_KEY_ID]
        artist = Artist(
            item_id=artist_id,
            name=jellyfin_artist[ITEM_KEY_NAME],
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if ITEM_KEY_OVERVIEW in jellyfin_artist:
            artist.metadata.description = jellyfin_artist[ITEM_KEY_OVERVIEW]
        if ITEM_KEY_MUSICBRAINZ_ARTIST in jellyfin_artist[ITEM_KEY_PROVIDER_IDS]:
            try:
                artist.mbid = jellyfin_artist[ITEM_KEY_PROVIDER_IDS][ITEM_KEY_MUSICBRAINZ_ARTIST]
            except InvalidDataError as error:
                self.logger.warning(
                    "Jellyfin has an invalid musicbrainz id for artist %s",
                    artist.name,
                    exc_info=error if self.logger.isEnabledFor(logging.DEBUG) else None,
                )
        if ITEM_KEY_SORT_NAME in jellyfin_artist:
            artist.sort_name = jellyfin_artist[ITEM_KEY_SORT_NAME]
        if thumb := self._get_thumbnail_url(jellyfin_artist):
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb,
                        provider=self.instance_id,
                        remotely_accessible=False,
                    )
                ]
            )
        user_data = jellyfin_artist.get(ITEM_KEY_USER_DATA, {})
        artist.favorite = user_data.get(USER_DATA_KEY_IS_FAVORITE, False)
        return artist

    async def _parse_track(self, jellyfin_track: JellyMediaItem) -> Track:
        """Parse a Jellyfin Track response to a Track model object."""
        available = False
        content = None
        available = jellyfin_track[ITEM_KEY_CAN_DOWNLOAD]
        content = jellyfin_track[ITEM_KEY_MEDIA_STREAMS][0][ITEM_KEY_MEDIA_CODEC]
        track = Track(
            item_id=jellyfin_track[ITEM_KEY_ID],
            provider=self.instance_id,
            name=jellyfin_track[ITEM_KEY_NAME],
            provider_mappings={
                ProviderMapping(
                    item_id=jellyfin_track[ITEM_KEY_ID],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=available,
                    audio_format=AudioFormat(
                        content_type=(
                            ContentType.try_parse(content) if content else ContentType.UNKNOWN
                        ),
                    ),
                    url=self._get_stream_url(jellyfin_track[ITEM_KEY_ID]),
                )
            },
        )

        if disc_number := jellyfin_track.get(ITEM_KEY_PARENT_INDEX_NUM):
            track.disc_number = disc_number
        if "IndexNumber" in jellyfin_track:
            if jellyfin_track["IndexNumber"] >= 1:
                track_idx = jellyfin_track["IndexNumber"]
                track.track_number = track_idx
                track.position = track_idx

        if thumb := self._get_thumbnail_url(jellyfin_track):
            track.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb,
                        provider=self.instance_id,
                        remotely_accessible=False,
                    )
                ]
            )

        if jellyfin_track[ITEM_KEY_ARTIST_ITEMS]:
            for artist_item in jellyfin_track[ITEM_KEY_ARTIST_ITEMS]:
                track.artists.append(
                    self._get_item_mapping(
                        MediaType.ARTIST,
                        artist_item[ITEM_KEY_ID],
                        artist_item[ITEM_KEY_NAME],
                    )
                )
        elif ITEM_KEY_ALBUM_ID in jellyfin_track:
            parent_album = await self._client.get_item(jellyfin_track[ITEM_KEY_ALBUM_ID])
            if ITEM_KEY_ALBUM_ARTISTS in parent_album:
                for artist_item in parent_album[ITEM_KEY_ALBUM_ARTISTS]:
                    track.artists.append(
                        self._get_item_mapping(
                            MediaType.ARTIST,
                            artist_item[ITEM_KEY_ID],
                            artist_item[ITEM_KEY_NAME],
                        )
                    )

        if ITEM_KEY_ALBUM_ID in jellyfin_track and ITEM_KEY_ALBUM in jellyfin_track:
            track.album = self._get_item_mapping(
                MediaType.ALBUM,
                jellyfin_track[ITEM_KEY_ALBUM_ID],
                jellyfin_track[ITEM_KEY_ALBUM],
            )
        elif ITEM_KEY_ALBUM_ID in jellyfin_track:
            parent_album = await self._client.get_item(jellyfin_track[ITEM_KEY_ALBUM_ID])
            track.album = self._get_item_mapping(
                MediaType.ALBUM,
                parent_album[ITEM_KEY_ID],
                parent_album[ITEM_KEY_NAME],
            )

        if ITEM_KEY_RUNTIME_TICKS in jellyfin_track:
            track.duration = int(
                jellyfin_track[ITEM_KEY_RUNTIME_TICKS] / 10000000
            )  # 10000000 ticks per millisecond
        if ITEM_KEY_MUSICBRAINZ_TRACK in jellyfin_track[ITEM_KEY_PROVIDER_IDS]:
            track_mbid = jellyfin_track[ITEM_KEY_PROVIDER_IDS][ITEM_KEY_MUSICBRAINZ_TRACK]
            try:
                track.mbid = track_mbid
            except InvalidDataError as error:
                self.logger.warning(
                    "Jellyfin has an invalid musicbrainz id for track %s",
                    track.name,
                    exc_info=error if self.logger.isEnabledFor(logging.DEBUG) else None,
                )
        user_data = jellyfin_track.get(ITEM_KEY_USER_DATA, {})
        track.favorite = user_data.get(USER_DATA_KEY_IS_FAVORITE, False)
        return track

    def _parse_playlist(self, jellyfin_playlist: JellyMediaItem) -> Playlist:
        """Parse a Jellyfin Playlist response to a Playlist object."""
        playlistid = jellyfin_playlist[ITEM_KEY_ID]
        playlist = Playlist(
            item_id=playlistid,
            provider=self.domain,
            name=jellyfin_playlist[ITEM_KEY_NAME],
            provider_mappings={
                ProviderMapping(
                    item_id=playlistid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if ITEM_KEY_OVERVIEW in jellyfin_playlist:
            playlist.metadata.description = jellyfin_playlist[ITEM_KEY_OVERVIEW]
        if thumb := self._get_thumbnail_url(jellyfin_playlist):
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb,
                        provider=self.instance_id,
                        remotely_accessible=False,
                    )
                ]
            )
        user_data = jellyfin_playlist.get(ITEM_KEY_USER_DATA, {})
        playlist.favorite = user_data.get(USER_DATA_KEY_IS_FAVORITE, False)
        playlist.is_editable = False
        return playlist

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 20,
    ) -> SearchResults:
        """Perform search on the plex library.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        artists = None
        albums = None
        tracks = None
        playlists = None

        async with TaskGroup() as tg:
            if MediaType.ARTIST in media_types:
                artists = tg.create_task(self._search_artist(search_query, limit))
            if MediaType.ALBUM in media_types:
                albums = tg.create_task(self._search_album(search_query, limit))
            if MediaType.TRACK in media_types:
                tracks = tg.create_task(self._search_track(search_query, limit))
            if MediaType.PLAYLIST in media_types:
                playlists = tg.create_task(self._search_playlist(search_query, limit))

        search_results = SearchResults()

        if artists:
            search_results.artists += artists.result()
        if albums:
            search_results.albums += albums.result()
        if tracks:
            search_results.tracks += tracks.result()
        if playlists:
            search_results.playlists += playlists.result()

        return search_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            offset = 0
            limit = 100

            response = await self._client.artists(
                jellyfin_library[ITEM_KEY_ID],
                start_index=offset,
                limit=limit,
                enable_user_data=True,
                fields=ARTIST_FIELDS,
            )
            for artist in response["Items"]:
                yield self._parse_artist(artist)

            while offset < response["TotalRecordCount"]:
                response = await self._client.artists(
                    jellyfin_library[ITEM_KEY_ID],
                    start_index=offset,
                    limit=limit,
                    enable_user_data=True,
                    fields=ARTIST_FIELDS,
                )
                for artist in response["Items"]:
                    yield self._parse_artist(artist)

                offset += limit

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            offset = 0
            limit = 100

            response = await self._client.albums(
                jellyfin_library[ITEM_KEY_ID],
                start_index=offset,
                limit=limit,
                enable_user_data=True,
                fields=ALBUM_FIELDS,
            )
            for artist in response["Items"]:
                yield self._parse_album(artist)

            while offset < response["TotalRecordCount"]:
                response = await self._client.albums(
                    jellyfin_library[ITEM_KEY_ID],
                    start_index=offset,
                    limit=limit,
                    enable_user_data=True,
                    fields=ALBUM_FIELDS,
                )
                for artist in response["Items"]:
                    yield self._parse_album(artist)

                offset += limit

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            albums = await self._client.albums(jellyfin_library[ITEM_KEY_ID])
            for album in albums["Items"]:
                tracks_obj = await self._get_children(album[ITEM_KEY_ID], ITEM_TYPE_AUDIO)
                for track in tracks_obj:
                    yield await self._parse_track(track)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlist_libraries = await self._get_playlists()
        for playlist_library in playlist_libraries:
            playlists_obj = await self._get_children(playlist_library[ITEM_KEY_ID], "Playlist")
            for playlist in playlists_obj:
                if "MediaType" in playlist:  # Only jellyfin has this property
                    if playlist["MediaType"] == "Audio":
                        yield self._parse_playlist(playlist)
                else:  # emby playlists are only audio type
                    yield self._parse_playlist(playlist)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        if jellyfin_album := await self._client.get_item(prov_album_id):
            return self._parse_album(jellyfin_album)
        msg = f"Item {prov_album_id} not found"
        raise MediaNotFoundError(msg)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        jellyfin_album_tracks = await self._get_children(prov_album_id, ITEM_TYPE_AUDIO)
        return [
            await self._parse_track(jellyfin_album_track)
            for jellyfin_album_track in jellyfin_album_tracks
        ]

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            # This artist does not exist in jellyfin, so we can just load it from DB.

            if db_artist := await self.mass.music.artists.get_library_item_by_prov_id(
                prov_artist_id, self.instance_id
            ):
                return db_artist
            msg = f"Artist not found: {prov_artist_id}"
            raise MediaNotFoundError(msg)

        if jellyfin_artist := await self._client.get_item(prov_artist_id):
            return self._parse_artist(jellyfin_artist)
        msg = f"Item {prov_artist_id} not found"
        raise MediaNotFoundError(msg)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if jellyfin_track := await self._client.get_item(prov_track_id):
            return await self._parse_track(jellyfin_track)
        msg = f"Item {prov_track_id} not found"
        raise MediaNotFoundError(msg)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if jellyfin_playlist := await self._client.get_item(prov_playlist_id):
            return self._parse_playlist(jellyfin_playlist)
        msg = f"Item {prov_playlist_id} not found"
        raise MediaNotFoundError(msg)

    async def get_playlist_tracks(
        self, prov_playlist_id: str, offset: int, limit: int
    ) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if offset:
            # paging not supported, we always return the whole list at once
            return []
        # TODO: Does Jellyfin support paging here?
        jellyfin_playlist = await self._client.get_item(prov_playlist_id)
        playlist_items = await self._get_children(jellyfin_playlist[ITEM_KEY_ID], ITEM_TYPE_AUDIO)
        if not playlist_items:
            return result
        for index, jellyfin_track in enumerate(playlist_items, 1):
            try:
                if track := await self._parse_track(jellyfin_track):
                    if not track.position:
                        track.position = offset + index
                    result.append(track)
            except (KeyError, ValueError) as err:
                self.logger.error(
                    "Skipping track %s: %s", jellyfin_track.get(ITEM_KEY_NAME, index), str(err)
                )
        return result

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        if not prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            return []
        albums = await self._client.album(
            prov_artist_id, fields=ALBUM_FIELDS, enable_user_data=True
        )
        return [self._parse_album(album) for album in albums["Items"]]

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        jellyfin_track = await self._client.get_item(item_id)
        mimetype = self._media_mime_type(jellyfin_track)
        media_stream = jellyfin_track[ITEM_KEY_MEDIA_STREAMS][0]
        url = self._client.audio_url(jellyfin_track[ITEM_KEY_ID], SUPPORTED_CONTAINER_FORMATS)
        if ITEM_KEY_MEDIA_CODEC in media_stream:
            content_type = ContentType.try_parse(media_stream[ITEM_KEY_MEDIA_CODEC])
        else:
            content_type = ContentType.try_parse(mimetype) if mimetype else ContentType.UNKNOWN
        return StreamDetails(
            item_id=jellyfin_track[ITEM_KEY_ID],
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=content_type,
                channels=jellyfin_track[ITEM_KEY_MEDIA_STREAMS][0][ITEM_KEY_MEDIA_CHANNELS],
            ),
            stream_type=StreamType.HTTP,
            duration=int(
                jellyfin_track[ITEM_KEY_RUNTIME_TICKS] / 10000000
            ),  # 10000000 ticks per millisecond)
            path=url,
        )

    def _get_thumbnail_url(self, media_item: JellyMediaItem) -> str | None:
        """Return the URL for the primary image of a media item if available."""
        image_tags = media_item[ITEM_KEY_IMAGE_TAGS]

        if "Primary" not in image_tags:
            return None

        item_id = media_item[ITEM_KEY_ID]
        return self._client.artwork(item_id, "Primary", MAX_IMAGE_WIDTH)

    def _get_stream_url(self, media_item: str) -> str:
        """Return the stream URL for a media item."""
        return self._client.audio_url(media_item)

    async def _get_children(self, parent_id: str, item_type: str) -> list[JellyMediaItem]:
        """Return all children for the parent_id whose item type is item_type."""
        params: dict[str, str | int] = {
            "Recursive": "true",
            ITEM_KEY_PARENT_ID: parent_id,
        }
        if item_type in ITEM_TYPE_ARTIST:
            params["IncludeItemTypes"] = f"{ITEM_TYPE_MUSICARTISTS},{ITEM_TYPE_ARTIST}"
        else:
            params["IncludeItemTypes"] = item_type
        if item_type in ITEM_TYPE_AUDIO:
            params["Fields"] = TRACK_FIELDS

        result = await self._client.user_items("", params)
        return result["Items"]

    async def _get_music_libraries(self) -> list[JellyMediaLibrary]:
        """Return all supported libraries a user has access to."""
        response = await self._client.get_media_folders()
        libraries = response["Items"]
        result = []
        for library in libraries:
            if ITEM_KEY_COLLECTION_TYPE in library and library[ITEM_KEY_COLLECTION_TYPE] in "music":
                result.append(library)
        return result

    async def _get_playlists(self) -> list[JellyMediaLibrary]:
        """Return all supported libraries a user has access to."""
        response = await self._client.get_media_folders()
        libraries = response["Items"]
        result = []
        for library in libraries:
            if (
                ITEM_KEY_COLLECTION_TYPE in library
                and library[ITEM_KEY_COLLECTION_TYPE] in "playlists"
            ):
                result.append(library)
        return result

    def _media_mime_type(self, media_item: JellyMediaItem) -> str | None:
        """Return the mime type of a media item."""
        if not media_item.get(ITEM_KEY_MEDIA_SOURCES):
            return None

        media_source = media_item[ITEM_KEY_MEDIA_SOURCES][0]

        if "Path" not in media_source:
            return None

        path = media_source["Path"]
        mime_type, _ = mimetypes.guess_type(path)

        return mime_type
