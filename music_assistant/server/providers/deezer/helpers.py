"""Helper module for parsing the Deezer API. Also helper for getting audio streams.

This helpers file is an async wrapper around the excellent deezer-python package.
While the deezer-python package does an excellent job at parsing the Deezer results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Deezer provider logic.

CREDITS:
deezer-python: https://github.com/browniebroke/deezer-python by @browniebroke
"""

import asyncio
import hashlib
from dataclasses import dataclass

import deezer
import deezer.exceptions
from Crypto.Cipher import Blowfish

from music_assistant.common.models.enums import AlbumType, ContentType, ImageType, MediaType
from music_assistant.common.models.errors import LoginFailed
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant.server.providers.deezer import GWClient


@dataclass
class Credential:
    """Class for storing credentials."""

    def __init__(self, app_id: int, app_secret: str, access_token: str):
        """Set the correct things."""
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = access_token

    app_id: int
    app_secret: str
    access_token: str


async def get_deezer_client(creds: Credential) -> deezer.Client:  # type: ignore
    """
    Return a deezer-python Client.

    If credentials are given the client is authorized.
    If no credentials are given the deezer client is not authorized.

    :param creds: Credentials. If none are given client is not authorized, defaults to None
    :type creds: credential, optional
    """
    if not isinstance(creds, Credential):
        raise TypeError("Creds must be of type credential")

    def _authorize():
        return deezer.Client(
            app_id=creds.app_id, app_secret=creds.app_secret, access_token=creds.access_token
        )

    return await asyncio.to_thread(_authorize)


async def get_artist(client: deezer.Client, artist_id: int) -> deezer.Artist:
    """Async wrapper of the deezer-python get_artist function."""

    def _get_artist():
        artist = client.get_artist(artist_id=artist_id)
        return artist

    return await asyncio.to_thread(_get_artist)


async def get_album(client: deezer.Client, album_id: int) -> deezer.Album:
    """Async wrapper of the deezer-python get_album function."""

    def _get_album():
        album = client.get_album(album_id=album_id)
        return album

    return await asyncio.to_thread(_get_album)


async def get_playlist(client: deezer.Client, playlist_id) -> deezer.Playlist:
    """Async wrapper of the deezer-python get_playlist function."""

    def _get_playlist():
        playlist = client.get_playlist(playlist_id=playlist_id)
        return playlist

    return await asyncio.to_thread(_get_playlist)


async def get_track(client: deezer.Client, track_id: int) -> deezer.Track:
    """Async wrapper of the deezer-python get_track function."""

    def _get_track():
        track = client.get_track(track_id=track_id)
        return track

    return await asyncio.to_thread(_get_track)


async def get_user_artists(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_artists function."""

    def _get_artist():
        artists = client.get_user_artists()
        return artists

    return await asyncio.to_thread(_get_artist)


async def get_user_playlists(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_playlists function."""

    def _get_playlist():
        playlists = client.get_user().get_playlists()
        return playlists

    return await asyncio.to_thread(_get_playlist)


async def get_user_albums(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_albums function."""

    def _get_album():
        albums = client.get_user_albums()
        return albums

    return await asyncio.to_thread(_get_album)


async def get_user_tracks(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_tracks function."""

    def _get_track():
        tracks = client.get_user_tracks()
        return tracks

    return await asyncio.to_thread(_get_track)


async def add_user_albums(client: deezer.Client, album_id: int) -> bool:
    """Async wrapper of the deezer-python add_user_albums function."""

    def _get_track():
        success = client.add_user_album(album_id=album_id)
        return success

    return await asyncio.to_thread(_get_track)


async def remove_user_albums(client: deezer.Client, album_id: int) -> bool:
    """Async wrapper of the deezer-python remove_user_albums function."""

    def _get_track():
        success = client.remove_user_album(album_id=album_id)
        return success

    return await asyncio.to_thread(_get_track)


async def add_user_tracks(client: deezer.Client, track_id: int) -> bool:
    """Async wrapper of the deezer-python add_user_tracks function."""

    def _get_track():
        success = client.add_user_track(track_id=track_id)
        return success

    return await asyncio.to_thread(_get_track)


async def remove_user_tracks(client: deezer.Client, track_id: int) -> bool:
    """Async wrapper of the deezer-python remove_user_tracks function."""

    def _get_track():
        success = client.remove_user_track(track_id=track_id)
        return success

    return await asyncio.to_thread(_get_track)


async def add_user_artists(client: deezer.Client, artist_id: int) -> bool:
    """Async wrapper of the deezer-python add_user_artists function."""

    def _get_artist():
        success = client.add_user_artist(artist_id=artist_id)
        return success

    return await asyncio.to_thread(_get_artist)


async def remove_user_artists(client: deezer.Client, artist_id: int) -> bool:
    """Async wrapper of the deezer-python remove_user_artists function."""

    def _get_artist():
        success = client.remove_user_artist(artist_id=artist_id)
        return success

    return await asyncio.to_thread(_get_artist)


async def search_album(client: deezer.Client, query: str, limit: int = 5) -> list[deezer.Album]:
    """Async wrapper of the deezer-python search_albums function."""

    def _search():
        result = client.search_albums(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


async def search_track(client: deezer.Client, query: str, limit: int = 5) -> list[deezer.Track]:
    """Async wrapper of the deezer-python search function."""

    def _search():
        result = client.search(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


async def search_artist(client: deezer.Client, query: str, limit: int = 5) -> list[deezer.Artist]:
    """Async wrapper of the deezer-python search_artist function."""

    def _search():
        result = client.search_artists(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


async def search_playlist(
    client: deezer.Client, query: str, limit: int = 5
) -> list[deezer.Playlist]:
    """Async wrapper of the deezer-python search_playlist function."""

    def _search():
        result = client.search_playlists(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


async def update_access_token(mass, app_id, app_secret, code) -> str:
    """Update the access_token."""
    response = await _post_http(
        http_session=mass.http_session,
        url="https://connect.deezer.com/oauth/access_token.php",
        data={
            "code": code,
            "app_id": app_id,
            "secret": app_secret,
        },
        params={
            "code": code,
            "app_id": app_id,
            "secret": app_secret,
        },
        headers=None,
    )
    try:
        return response.split("=")[1].split("&")[0]
    except Exception as error:
        raise LoginFailed("Invalid auth code") from error


async def _post_http(http_session, url, data, params=None, headers=None) -> str:
    async with http_session.post(
        url, headers=headers, params=params, json=data, ssl=False
    ) as response:
        if response.status != 200:
            raise ConnectionError(f"HTTP Error {response.status}: {response.reason}")
        response_text = await response.text()
        return response_text


def parse_artist(mass, artist: deezer.Artist) -> Artist:
    """Parse the deezer-python artist to a MASS artist."""
    return Artist(
        item_id=str(artist.id),
        provider=mass.domain,
        name=artist.name,
        media_type=MediaType.ARTIST,
        provider_mappings={
            ProviderMapping(
                item_id=str(artist.id),
                provider_domain=mass.domain,
                provider_instance=mass.instance_id,
            )
        },
        metadata=parse_metadata_artist(artist=artist),
    )


def parse_album(mass, album: deezer.Album) -> Album:
    """Parse the deezer-python album to a MASS album."""
    return Album(
        album_type=AlbumType(album.type),
        item_id=str(album.id),
        provider=mass.domain,
        name=album.title,
        artists=[parse_artist(mass=mass, artist=album.get_artist())],
        media_type=MediaType.ALBUM,
        provider_mappings={
            ProviderMapping(
                item_id=str(album.id),
                provider_domain=mass.domain,
                provider_instance=mass.instance_id,
            )
        },
        metadata=parse_metadata_album(album=album),
    )


def parse_playlist(mass, playlist: deezer.Playlist) -> Playlist:
    """Parse the deezer-python playlist to a MASS playlist."""
    return Playlist(
        item_id=str(playlist.id),
        provider=mass.domain,
        name=playlist.title,
        media_type=MediaType.PLAYLIST,
        provider_mappings={
            ProviderMapping(
                item_id=str(playlist.id),
                provider_domain=mass.domain,
                provider_instance=mass.instance_id,
            )
        },
        metadata=MediaItemMetadata(
            images=[MediaItemImage(type=ImageType.THUMB, path=playlist.picture_big)],
        ),
    )


def parse_metadata_track(track: deezer.Track) -> MediaItemMetadata:
    """Parse the track metadata."""
    try:
        return MediaItemMetadata(
            preview=track.preview,
            images=[
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=track.get_album().cover_big,
                )
            ],
        )
    except AttributeError:
        return MediaItemMetadata(
            preview=track.preview,
        )


def parse_metadata_album(album: deezer.Album) -> MediaItemMetadata:
    """Parse the album metadata."""
    return MediaItemMetadata(
        images=[MediaItemImage(type=ImageType.THUMB, path=album.cover_big)],
    )


def parse_metadata_artist(artist: deezer.Artist) -> MediaItemMetadata:
    """Parse the artist metadata."""
    return MediaItemMetadata(
        images=[MediaItemImage(type=ImageType.THUMB, path=artist.picture_big)],
    )


def _get_album(mass, track: deezer.Track) -> Album | None:
    try:
        return parse_album(mass=mass, album=track.get_album())
    except AttributeError:
        return None


async def get_album_from_track(track: deezer.Track) -> deezer.Album:
    """Get track's artist."""

    def _get_album_from_track():
        try:
            return track.get_album()
        except deezer.exceptions.DeezerErrorResponse:
            return None

    return await asyncio.to_thread(_get_album_from_track)


async def get_artist_from_track(track: deezer.Track) -> deezer.Artist:
    """Get track's artist."""

    def _get_artist_from_track():
        return track.get_artist()

    return await asyncio.to_thread(_get_artist_from_track)


async def get_artist_from_album(album: deezer.Album) -> deezer.Artist:
    """Get track's artist."""

    def _get_artist_from_album():
        return album.get_artist()

    return await asyncio.to_thread(_get_artist_from_album)


async def get_albums_by_artist(artist: deezer.Artist) -> deezer.PaginatedList:
    """Get albums by an artist."""

    def _get_albums_by_artist():
        return artist.get_albums()

    return await asyncio.to_thread(_get_albums_by_artist)


async def get_artist_top(artist: deezer.Artist) -> deezer.PaginatedList:
    """Get top tracks by an artist."""

    def _get_artist_top():
        return artist.get_top()

    return await asyncio.to_thread(_get_artist_top)


async def get_track_content_type(gw_client: GWClient, track_id: int):
    """Get a tracks contentType."""
    song_data = await gw_client.get_song_data(track_id)
    if song_data["results"]["FILESIZE_FLAC"]:
        return ContentType.FLAC

    if song_data["results"]["FILESIZE_MP3_320"] or song_data["results"]["FILESIZE_MP3_128"]:
        return ContentType.MP3

    raise NotImplementedError("Unsupported contenttype")


def parse_track(mass, track: deezer.Track) -> Track:
    """Parse the deezer-python track to a MASS track."""
    artist = track.get_artist()
    return Track(
        item_id=str(track.id),
        provider=mass.domain,
        name=track.title,
        media_type=MediaType.TRACK,
        sort_name=track.title_short,
        position=track.track_position,
        duration=track.duration,
        artists=[parse_artist(mass=mass, artist=artist)],
        album=track.get_album(),
        provider_mappings={
            ProviderMapping(
                item_id=str(track.id),
                provider_domain=mass.domain,
                provider_instance=mass.instance_id,
            )
        },
        metadata=parse_metadata_track(track=track),
    )


async def search_and_parse_tracks(
    mass, client: deezer.Client, query: str, limit: int = 5
) -> list[Track]:
    """Search for tracks and parse them."""
    deezer_tracks = await search_track(client=client, query=query, limit=limit)
    return [parse_track(track=track, mass=mass) for track in deezer_tracks]


async def search_and_parse_artists(
    mass, client: deezer.Client, query: str, limit: int = 5
) -> list[Artist]:
    """Search for artists and parse them."""
    deezer_artist = await search_artist(client=client, query=query, limit=limit)
    return [parse_artist(artist=artist, mass=mass) for artist in deezer_artist]


async def search_and_parse_albums(
    mass, client: deezer.Client, query: str, limit: int = 5
) -> list[Album]:
    """Search for album and parse them."""
    deezer_albums = await search_album(client=client, query=query, limit=limit)
    return [parse_album(album=album, mass=mass) for album in deezer_albums]


async def search_and_parse_playlists(
    mass, client: deezer.Client, query: str, limit: int = 5
) -> list[Playlist]:
    """Search for playlists and parse them."""
    deezer_playlists = await search_playlist(client=client, query=query, limit=limit)
    return [parse_playlist(playlist=playlist, mass=mass) for playlist in deezer_playlists]


def _md5(data, data_type="ascii"):
    md5sum = hashlib.md5()
    md5sum.update(data.encode(data_type))
    return md5sum.hexdigest()


def get_blowfish_key(track_id):
    """Get blowfish key to decrypt a chunk of a track."""
    secret = "g4el58wc" + "0zvf9na1"
    id_md5 = _md5(track_id)
    bf_key = ""
    for i in range(16):
        bf_key += chr(ord(id_md5[i]) ^ ord(id_md5[i + 16]) ^ ord(secret[i]))
    return bf_key


def decrypt_chunk(chunk, blowfish_key):
    """Decrypt a given chunk using the blow fish key."""
    cipher = Blowfish.new(
        blowfish_key.encode("ascii"), Blowfish.MODE_CBC, b"\x00\x01\x02\x03\x04\x05\x06\x07"
    )
    return cipher.decrypt(chunk)
