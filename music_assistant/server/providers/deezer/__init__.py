"""Deezer music provider support for MusicAssistant."""
import os
from asyncio import TaskGroup
from collections.abc import AsyncGenerator

import deezer
from asyncio_throttle.throttler import Throttler

from music_assistant.common.models.config_entries import ConfigEntry, ProviderConfig
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
)
from music_assistant.common.models.errors import LoginFailed
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    MediaItemType,
    Playlist,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.server.helpers.process import AsyncProcess
from music_assistant.server.models import ProviderInstanceType
from music_assistant.server.models.music_provider import MusicProvider
from music_assistant.server.server import MusicAssistant

from .helpers import (
    Credential,
    add_user_albums,
    add_user_artists,
    add_user_tracks,
    get_album,
    get_albums_by_artist,
    get_artist,
    get_artist_top,
    get_deezer_client,
    get_playlist,
    get_track,
    get_user_albums,
    get_user_artists,
    get_user_playlists,
    get_user_tracks,
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
    remove_user_albums,
    remove_user_artists,
    remove_user_tracks,
    search_and_parse_album,
    search_and_parse_artist,
    search_and_parse_playlist,
    search_and_parse_track,
    update_access_token,
)

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
)

CONF_AUTHORIZATION_CODE = "authorization_code"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = DeezerProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant, manifest: ProviderManifest  # noqa: ARG001 # pylint: disable=W0613
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_AUTHORIZATION_CODE,
            type=ConfigEntryType.STRING,
            label="Authorization code",
            required=True,
            description="The auth code u got from deezer",
        ),
    )


class DeezerProvider(MusicProvider):
    """Deezer provider support."""

    client: deezer.Client
    creds: Credential
    _throttler: Throttler

    async def handle_setup(self) -> None:
        """Set up the Deezer provider."""
        auth_token = f"custom_data/{self.instance_id}/auth"
        self._throttler = Throttler(rate_limit=4, period=1)
        self.creds = Credential(
            app_id=587964,
            app_secret="3725582e5aeec225901e4eb03684dbfb",
        )
        if auth_encrypted := self.mass.config.get(auth_token):
            auth = self.mass.config.decrypt_string(auth_encrypted)
            self.creds.access_token = auth
        else:
            code = str(self.config.get_value(CONF_AUTHORIZATION_CODE))
            # Reset auth code in config since its one time
            self.mass.config.set(f"{self.instance_id}{CONF_AUTHORIZATION_CODE}", "")
            self.creds = await update_access_token(mass=self, creds=self.creds, code=code)
            self.mass.config.set(
                key=auth_token, value=self.mass.config.encrypt_string(self.creds.access_token)
            )
        try:
            self.client = await get_deezer_client(creds=self.creds)
        except Exception as error:
            raise LoginFailed("Invalid login credentials") from error
        # Reset auth code since its one time
        self.mass.config.set(f"{self.instance_id}{CONF_AUTHORIZATION_CODE}", "")

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    @property
    def is_unique(self) -> bool:
        """
        Return True if the (non user related) data in this provider instance is unique.

        For example on a global streaming provider (like Spotify),
        the data on all instances is the same.
        For a file provider each instance has other items.
        Setting this to False will only query one instance of the provider for search and lookups.
        Setting this to True will query all instances of this provider for search and lookups.
        """
        return False

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 5
    ) -> SearchResults:
        """Perform search on music provider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        """
        if not media_types:
            media_types = [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK, MediaType.PLAYLIST]

        tasks = {}

        async with TaskGroup() as taskgroup:
            for media_type in media_types:
                if media_type == MediaType.TRACK:
                    tasks[MediaType.TRACK] = taskgroup.create_task(
                        search_and_parse_track(
                            mass=self, client=self.client, query=search_query, limit=limit
                        )
                    )
                elif media_type == MediaType.ARTIST:
                    tasks[MediaType.ARTIST] = taskgroup.create_task(
                        search_and_parse_artist(
                            mass=self, client=self.client, query=search_query, limit=limit
                        )
                    )
                elif media_type == MediaType.ALBUM:
                    tasks[MediaType.ALBUM] = taskgroup.create_task(
                        search_and_parse_album(
                            mass=self, client=self.client, query=search_query, limit=limit
                        )
                    )
                elif media_type == MediaType.PLAYLIST:
                    tasks[MediaType.PLAYLIST] = taskgroup.create_task(
                        search_and_parse_playlist(
                            mass=self, client=self.client, query=search_query, limit=limit
                        )
                    )

        results = SearchResults()

        for media_type, task in tasks.items():
            if media_type == MediaType.ARTIST:
                results.artists = task.result()
            elif media_type == MediaType.ALBUM:
                results.albums = task.result()
            elif media_type == MediaType.TRACK:
                results.tracks = task.result()
            elif media_type == MediaType.PLAYLIST:
                results.playlists = task.result()

        return results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Deezer."""
        for artist in await get_user_artists(client=self.client):
            yield await parse_artist(mass=self, artist=artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Deezer."""
        for album in await get_user_albums(client=self.client):
            yield await parse_album(mass=self, album=album)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from Deezer."""
        for playlist in await get_user_playlists(client=self.client):
            yield await parse_playlist(mass=self, playlist=playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve all library tracks from Deezer."""
        for track in await get_user_tracks(client=self.client):
            yield await parse_track(mass=self, track=track)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return await parse_artist(
            mass=self, artist=await get_artist(client=self.client, artist_id=int(prov_artist_id))
        )

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return await parse_album(
            mass=self, album=await get_album(client=self.client, album_id=int(prov_album_id))
        )

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        return await parse_playlist(
            mass=self,
            playlist=await get_playlist(client=self.client, playlist_id=int(prov_playlist_id)),
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return await parse_track(
            mass=self, track=await get_track(client=self.client, track_id=int(prov_track_id))
        )

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all albums in a playlist."""
        album = await get_album(client=self.client, album_id=int(prov_album_id))
        tracks = []
        for track in album.tracks:
            tracks.append(await parse_track(mass=self, track=track))
        return tracks

    async def get_playlist_tracks(self, prov_playlist_id: str) -> AsyncGenerator[Track, None]:
        """Get all tracks in a playlist."""
        playlist = await get_playlist(client=self.client, playlist_id=prov_playlist_id)
        for track in playlist.tracks:
            yield await parse_track(mass=self, track=track)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        artist = await get_artist(client=self.client, artist_id=int(prov_artist_id))
        albums = []
        for album in await get_albums_by_artist(artist=artist):
            albums.append(await parse_album(mass=self, album=album))
        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top 25 tracks of an artist."""
        artist = await get_artist(client=self.client, artist_id=int(prov_artist_id))
        top_tracks = (await get_artist_top(artist=artist))[:25]
        return [await parse_track(mass=self, track=track) for track in top_tracks]

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add an item to the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await add_user_artists(
                artist_id=int(prov_item_id),
                client=self.client,
            )
        elif media_type == MediaType.ALBUM:
            result = await add_user_albums(
                album_id=int(prov_item_id),
                client=self.client,
            )
        elif media_type == MediaType.TRACK:
            result = await add_user_tracks(
                track_id=int(prov_item_id),
                client=self.client,
            )
        else:
            raise NotImplementedError
        return result

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove an item to the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await remove_user_artists(
                artist_id=int(prov_item_id),
                client=self.client,
            )
        elif media_type == MediaType.ALBUM:
            result = await remove_user_albums(
                album_id=int(prov_item_id),
                client=self.client,
            )
        elif media_type == MediaType.TRACK:
            result = await remove_user_tracks(
                track_id=int(prov_item_id),
                client=self.client,
            )
        else:
            raise NotImplementedError
        return result

    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        """Return the content details for the given track when it will be streamed."""
        track = await get_track(client=self.client, track_id=int(item_id))
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            content_type=ContentType.MP3,
            duration=track.duration,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        print("Is this running??")
        base_path = os.path.join(os.path.dirname(__file__), "dzr")
        args = [
            f"{base_path}/get-bytes.sh",
            streamdetails.item_id,
        ]
        print(seek_position)
        async with AsyncProcess(args) as dzr_proc:
            async for chunk in dzr_proc.iter_any():
                print(chunk)
                yield chunk

    async def resolve_image(self, path: str) -> str | bytes | AsyncGenerator[bytes, None]:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        NOT IMPLEMENTED
        """
        raise NotImplementedError

    async def get_item(self, media_type: MediaType, prov_item_id: str) -> MediaItemType:
        """Get single MediaItem from provider."""
        if media_type == MediaType.ARTIST:
            return await self.get_artist(prov_item_id)
        if media_type == MediaType.ALBUM:
            return await self.get_album(prov_item_id)
        if media_type == MediaType.PLAYLIST:
            return await self.get_playlist(prov_item_id)
        return await self.get_track(prov_item_id)
