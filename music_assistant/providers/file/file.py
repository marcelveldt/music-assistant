"""Filesystem musicprovider support for MusicAssistant."""
import base64
import os
from typing import List

from music_assistant.constants import CONF_ENABLED
from music_assistant.models.media_types import (
    Album,
    Artist,
    MediaType,
    Playlist,
    Track,
    TrackQuality,
)
from music_assistant.models.musicprovider import MusicProvider
from music_assistant.utils import LOGGER, parse_title_and_version
import taglib

PROV_NAME = "Local files and playlists"
PROV_CLASS = "FileProvider"

CONFIG_ENTRIES = [
    (CONF_ENABLED, False, CONF_ENABLED),
    ("music_dir", "", "file_prov_music_path"),
    ("playlists_dir", "", "file_prov_playlists_path"),
]


class FileProvider(MusicProvider):
    """
        Very basic implementation of a musicprovider for local files
        Assumes files are stored on disk in format <artist>/<album>/<track.ext>
        Reads ID3 tags from file and falls back to parsing filename
        Supports m3u files only for playlists
        Supports having URI's from streaming providers within m3u playlist
        Should be compatible with LMS
    """

    _music_dir = None
    _playlists_dir = None

    async def async_setup(self, conf):
        """setup the provider, return True if succesfull"""
        if not os.path.isdir(conf["music_dir"]):
            raise FileNotFoundError(f"Directory {conf['music_dir']} does not exist")
        self._music_dir = conf["music_dir"]
        if os.path.isdir(conf["playlists_dir"]):
            self._playlists_dir = conf["playlists_dir"]
        else:
            self._playlists_dir = None

    async def async_search(self, searchstring, media_types=List[MediaType], limit=5):
        """perform search on the provider"""
        result = {"artists": [], "albums": [], "tracks": [], "playlists": []}
        return result

    async def async_get_library_artists(self) -> List[Artist]:
        """get artist folders in music directory"""
        if not os.path.isdir(self._music_dir):
            LOGGER.error("music path does not exist: %s" % self._music_dir)
            return
            yield
        for dirname in os.listdir(self._music_dir):
            dirpath = os.path.join(self._music_dir, dirname)
            if os.path.isdir(dirpath) and not dirpath.startswith("."):
                artist = await self.get_artist(dirpath)
                if artist:
                    yield artist

    async def async_get_library_albums(self) -> List[Album]:
        """get album folders recursively"""
        async for artist in self.get_library_artists():
            async for album in self.get_artist_albums(artist.item_id):
                yield album

    async def async_get_library_tracks(self) -> List[Track]:
        """get all tracks recursively"""
        # TODO: support disk subfolders
        async for album in self.get_library_albums():
            async for track in self.get_album_tracks(album.item_id):
                yield track

    async def async_get_library_playlists(self) -> List[Playlist]:
        """retrieve playlists from disk"""
        if not self._playlists_dir:
            return
            yield
        for filename in os.listdir(self._playlists_dir):
            filepath = os.path.join(self._playlists_dir, filename)
            if (
                os.path.isfile(filepath)
                and not filename.startswith(".")
                and filename.lower().endswith(".m3u")
            ):
                playlist = await self.get_playlist(filepath)
                if playlist:
                    yield playlist

    async def async_get_artist(self, prov_item_id) -> Artist:
        """get full artist details by id"""
        if not os.sep in prov_item_id:
            itempath = base64.b64decode(prov_item_id).decode("utf-8")
        else:
            itempath = prov_item_id
            prov_item_id = base64.b64encode(itempath.encode("utf-8")).decode("utf-8")
        if not os.path.isdir(itempath):
            LOGGER.error("artist path does not exist: %s" % itempath)
            return None
        name = itempath.split(os.sep)[-1]
        artist = Artist()
        artist.item_id = prov_item_id
        artist.provider = self.prov_id
        artist.name = name
        artist.ids.append(
            {"provider": self.prov_id, "item_id": artist.item_id}
        )
        return artist

    async def async_get_album(self, prov_item_id) -> Album:
        """get full album details by id"""
        if not os.sep in prov_item_id:
            itempath = base64.b64decode(prov_item_id).decode("utf-8")
        else:
            itempath = prov_item_id
            prov_item_id = base64.b64encode(itempath.encode("utf-8")).decode("utf-8")
        if not os.path.isdir(itempath):
            LOGGER.error("album path does not exist: %s" % itempath)
            return None
        name = itempath.split(os.sep)[-1]
        artistpath = itempath.rsplit(os.sep, 1)[0]
        album = Album()
        album.item_id = prov_item_id
        album.provider = self.prov_id
        album.name, album.version = parse_title_and_version(name)
        album.artist = await self.get_artist(artistpath)
        if not album.artist:
            raise Exception("No album artist ! %s" % artistpath)
        album.ids.append({"provider": self.prov_id, "item_id": prov_item_id})
        return album

    async def async_get_track(self, prov_item_id) -> Track:
        """get full track details by id"""
        if not os.sep in prov_item_id:
            itempath = base64.b64decode(prov_item_id).decode("utf-8")
        else:
            itempath = prov_item_id
        if not os.path.isfile(itempath):
            LOGGER.error("track path does not exist: %s" % itempath)
            return None
        return await self.__parse_track(itempath)

    async def async_get_playlist(self, prov_item_id) -> Playlist:
        """get full playlist details by id"""
        if not os.sep in prov_item_id:
            itempath = base64.b64decode(prov_item_id).decode("utf-8")
        else:
            itempath = prov_item_id
            prov_item_id = base64.b64encode(itempath.encode("utf-8")).decode("utf-8")
        if not os.path.isfile(itempath):
            LOGGER.error("playlist path does not exist: %s" % itempath)
            return None
        playlist = Playlist()
        playlist.item_id = prov_item_id
        playlist.provider = self.prov_id
        playlist.name = itempath.split(os.sep)[-1].replace(".m3u", "")
        playlist.is_editable = True
        playlist.ids.append(
            {"provider": self.prov_id, "item_id": prov_item_id}
        )
        playlist.owner = "disk"
        playlist.checksum = os.path.getmtime(itempath)
        return playlist

    async def async_get_album_tracks(self, prov_album_id) -> List[Track]:
        """get album tracks for given album id"""
        if not os.sep in prov_album_id:
            albumpath = base64.b64decode(prov_album_id).decode("utf-8")
        else:
            albumpath = prov_album_id
        if not os.path.isdir(albumpath):
            LOGGER.error("album path does not exist: %s" % albumpath)
            return
        album = await self.get_album(albumpath)
        for filename in os.listdir(albumpath):
            filepath = os.path.join(albumpath, filename)
            if os.path.isfile(filepath) and not filepath.startswith("."):
                track = await self.__parse_track(filepath)
                if track:
                    track.album = album
                    yield track

    async def async_get_playlist_tracks(
        self, prov_playlist_id, limit=50, offset=0
    ) -> List[Track]:
        """get playlist tracks for given playlist id"""
        if not os.sep in prov_playlist_id:
            itempath = base64.b64decode(prov_playlist_id).decode("utf-8")
        else:
            itempath = prov_playlist_id
        if not os.path.isfile(itempath):
            LOGGER.error("playlist path does not exist: %s" % itempath)
            return
        counter = 0
        index = 0
        with open(itempath) as f:
            for line in f.readlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    counter += 1
                    if counter > offset:
                        track = await self.__parse_track_from_uri(line)
                        if track:
                            yield track
                            index += 1
                    if limit and index == limit:
                        break

    async def async_get_artist_albums(self, prov_artist_id) -> List[Album]:
        """get a list of albums for the given artist"""
        if not os.sep in prov_artist_id:
            artistpath = base64.b64decode(prov_artist_id).decode("utf-8")
        else:
            artistpath = prov_artist_id
        if not os.path.isdir(artistpath):
            LOGGER.error("artist path does not exist: %s" % artistpath)
            return
        for dirname in os.listdir(artistpath):
            dirpath = os.path.join(artistpath, dirname)
            if os.path.isdir(dirpath) and not dirpath.startswith("."):
                album = await self.get_album(dirpath)
                if album:
                    yield album

    async def async_get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        """get a list of random tracks as we have no clue about preference"""
        async for album in self.get_artist_albums(prov_artist_id):
            async for track in self.get_album_tracks(album.item_id):
                yield track

    async def async_get_stream_details(self, track_id):
        """return the content details for the given track when it will be streamed"""
        if not os.sep in track_id:
            track_id = base64.b64decode(track_id).decode("utf-8")
        if not os.path.isfile(track_id):
            return None
        # TODO: retrieve sanple rate and bitdepth
        return {
            "type": "file",
            "path": track_id,
            "content_type": track_id.split(".")[-1],
            "sample_rate": 44100,
            "bit_depth": 16,
        }

    async def __async_parse_track(self, filename):
        """try to parse a track from a filename with taglib"""
        track = Track()
        try:
            song = taglib.File(filename)
        except:
            return None  # not a media file ?
        prov_item_id = base64.b64encode(filename.encode("utf-8")).decode("utf-8")
        track.duration = song.length
        track.item_id = prov_item_id
        track.provider = self.prov_id
        name = song.tags["TITLE"][0]
        track.name, track.version = parse_title_and_version(name)
        albumpath = filename.rsplit(os.sep, 1)[0]
        track.album = await self.get_album(albumpath)
        artists = []
        for artist_str in song.tags["ARTIST"]:
            local_artist_path = os.path.join(self._music_dir, artist_str)
            if os.path.isfile(local_artist_path):
                artist = await self.get_artist(local_artist_path)
            else:
                artist = Artist()
                artist.name = artist_str
                fake_artistpath = os.path.join(self._music_dir, artist_str)
                artist.item_id = fake_artistpath  # temporary id
                artist.ids.append(
                    {
                        "provider": self.prov_id,
                        "item_id": base64.b64encode(
                            fake_artistpath.encode("utf-8")
                        ).decode("utf-8"),
                    }
                )
            artists.append(artist)
        track.artists = artists
        if "GENRE" in song.tags:
            track.tags = song.tags["GENRE"]
        if "ISRC" in song.tags:
            track.external_ids["isrc"] = song.tags["ISRC"][0]
        if "DISCNUMBER" in song.tags:
            track.disc_number = int(song.tags["DISCNUMBER"][0])
        if "TRACKNUMBER" in song.tags:
            track.track_number = int(song.tags["TRACKNUMBER"][0])
        quality_details = ""
        if filename.endswith(".flac"):
            # TODO: get bit depth
            quality = TrackQuality.FLAC_LOSSLESS
            if song.sampleRate > 192000:
                quality = TrackQuality.FLAC_LOSSLESS_HI_RES_4
            elif song.sampleRate > 96000:
                quality = TrackQuality.FLAC_LOSSLESS_HI_RES_3
            elif song.sampleRate > 48000:
                quality = TrackQuality.FLAC_LOSSLESS_HI_RES_2
            quality_details = "%s Khz" % (song.sampleRate / 1000)
        elif filename.endswith(".ogg"):
            quality = TrackQuality.LOSSY_OGG
            quality_details = "%s kbps" % (song.bitrate)
        elif filename.endswith(".m4a"):
            quality = TrackQuality.LOSSY_AAC
            quality_details = "%s kbps" % (song.bitrate)
        else:
            quality = TrackQuality.LOSSY_MP3
            quality_details = "%s kbps" % (song.bitrate)
        track.ids.append(
            {
                "provider": self.prov_id,
                "item_id": prov_item_id,
                "quality": quality,
                "details": quality_details,
            }
        )
        return track

    async def __async_parse_track_from_uri(self, uri):
        """try to parse a track from an uri found in playlist"""
        if "://" in uri:
            # track is uri from external provider?
            prov_id = uri.split("://")[0]
            prov_item_id = uri.split("/")[-1].split(".")[0].split(":")[-1]
            try:
                return await self.mass.music_manager.providers[prov_id].track(
                    prov_item_id, lazy=False
                )
            except Exception as exc:
                LOGGER.warning("Could not parse uri %s to track: %s" % (uri, str(exc)))
                return None
        # try to treat uri as filename
        # TODO: filename could be related to musicdir or full path
        track = await self.get_track(uri)
        if track:
            return track
        track = await self.get_track(os.path.join(self._music_dir, uri))
        if track:
            return track
        return None
