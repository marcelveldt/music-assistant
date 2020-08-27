"""Playermanager: Orchestrates all data from music providers and sync to internal database."""

import base64
import functools
import operator
import os
import time
from typing import List

from PIL import Image
import aiohttp
from music_assistant.constants import EVENT_MUSIC_SYNC_STATUS
from music_assistant.models.media_types import (
    Album,
    Artist,
    MediaItem,
    MediaType,
    Playlist,
    Radio,
    Track,
    ExternalId
)
from music_assistant.utils import LOGGER, run_periodic
import toolz
from typing import Optional
from music_assistant.models.provider import ProviderType
from music_assistant.cache import async_cached


def sync_task(desc):
    """Decorator to report a sync task."""

    def wrapper(func):
        @functools.wraps(func)
        async def wrapped(*args):
            method_class = args[0]
            prov_id = args[1]
            # check if this sync task is not already running
            for sync_prov_id, sync_desc in method_class.running_sync_jobs:
                if sync_prov_id == prov_id and sync_desc == desc:
                    LOGGER.warning(
                        "Syncjob %s for provider %s is already running!", desc, prov_id
                    )
                    return
            sync_job = (prov_id, desc)
            method_class.running_sync_jobs.append(sync_job)
            await method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs
            )
            await func(*args)
            LOGGER.info("Finished syncing %s for provider %s", desc, prov_id)
            method_class.running_sync_jobs.remove(sync_job)
            await method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs
            )

        return wrapped

    return wrapper


class MusicManager:
    """Several helpers around the musicproviders."""

    def __init__(self, mass):
        self.running_sync_jobs = []
        self.mass = mass
        self.cache = mass.cache
        self.providers = {}

    async def async_setup(self):
        """async initialize of module"""
        # load providers
        # await self.load_modules()
        # schedule sync task
        self.mass.loop.create_task(self.__async_sync_music_providers())


##########################


    async def async_get_artist(self,
                               item_id: str,
                               provider_id: str,
                               lazy: bool = True) -> Artist:
        """Return artist details for the given provider artist id."""
        item_id = await self.mass.database.async_get_database_id(
            provider_id, item_id, MediaType.Artist
        )
        if item_id is None:
            # artist not yet in local database so fetch details
            provider = self.mass.get_provider(provider_id)
            cache_key = f"{provider_id}.get_artist.{item_id}"
            artist = await async_cached(
                self.cache, cache_key, provider.async_get_artist(item_id)
            )
            if not artist:
                raise Exception("artist not found: %s" % item_id)
            if lazy:
                self.mass.create_task(self.async_add_artist(artist))
                artist.is_lazy = True
                return artist
            item_id = await self.async_add_artist(artist)
        return await self.mass.database.async_get_artist(item_id)

    async def async_add_artist(self, artist: Artist) -> int:
        """Add artist to local db and return the new database id."""
        musicbrainz_id = artist.external_ids.get(ExternalId.MUSICBRAINZ)
        if not musicbrainz_id:
            musicbrainz_id = await self.async_get_artist_musicbrainz_id(artist)
        if not musicbrainz_id:
            LOGGER.error("Abort adding artist %s to db because of missing musicbrainz id.",
                         artist.name)
            return
        # grab additional metadata
        artist.external_ids[ExternalId.MUSICBRAINZ] = musicbrainz_id
        artist.metadata = await self.mass.metadata.async_get_artist_metadata(
            musicbrainz_id, artist.metadata)
        item_id = await self.mass.database.async_add_artist(artist)
        # also fetch same artist on all providers
        new_artist = await self.mass.database.async_get_artist(item_id)
        new_artist_toptracks = [
            item async for item in self.async_get_artist_toptracks(artist.item_id, artist.provider)
        ]
        new_artist_albums = [
            item async for item in self.async_get_artist_albums(artist.item_id, artist.provider)
        ]
        if new_artist_toptracks or new_artist_albums:
            item_provider_keys = [item.provider for item in new_artist.provider_ids]
            for provider in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
                if not provider.id in item_provider_keys:
                    await self.async_match_artist(
                        new_artist, new_artist_albums, new_artist_toptracks
                    )
        return item_id

    async def async_get_artist_musicbrainz_id(self, artist: Artist):
        """Fetch musicbrainz id by performing search using the artist name, albums and tracks."""
        # try with album first
        async for lookup_album in self.async_get_artist_albums(artist.item_id, artist.provider):
            if not lookup_album:
                continue
            musicbrainz_id = await self.mass.metadata.async_get_mb_artist_id(
                artist.name,
                albumname=lookup_album.name,
                album_upc=lookup_album.external_ids.get(ExternalId.UPC))
            if musicbrainz_id:
                return musicbrainz_id
        # fallback to track
        async for lookup_track in self.async_get_artist_toptracks(artist.item_id, artist.provider):
            if not lookup_track:
                continue
            musicbrainz_id = await self.mass.metadata.async_get_mb_artist_id(
                artist.name,
                trackname=lookup_track.name,
                track_isrc=lookup_track.external_ids.get(ExternalId.ISRC))
            if musicbrainz_id:
                return musicbrainz_id
        # lookup failed, use the shitty workaround to use the name as id.
        LOGGER.warning("Unable to get musicbrainz ID for artist %s !", artist.name)
        return artist.name

    async def async_get_album(
        self, prov_item_id, lazy=True, album_details=None, provider=None
    ) -> Album:
        """return album details for the given provider album id"""
        if not provider:
            provider = self.id
        item_id = await self.mass.database.async_get_database_id(
            provider, prov_item_id, MediaType.Album
        )
        if item_id is None:
            # album not yet in local database so fetch details
            if not album_details:
                cache_key = f"{self.id}.get_album.{prov_item_id}"
                album_details = await cached(
                    self.cache, cache_key, self.get_album, prov_item_id
                )
            if not album_details:
                raise Exception("album not found: %s" % prov_item_id)
            if lazy:
                asyncio.create_task(self.add_album(album_details))
                album_details.is_lazy = True
                return album_details
            item_id = await self.add_album(album_details)
        return await self.mass.database.async_get_album(item_id)

    async def async_add_album(self, album_details) -> int:
        """add album to local db and return the new database id"""
        # we need to fetch album artist too
        db_album_artist = await self.artist(
            album_details.artist.item_id,
            lazy=False,
            ref_album=album_details,
            provider=album_details.artist.provider,
        )
        album_details.artist = db_album_artist
        item_id = await self.mass.database.async_add_album(album_details)
        # also fetch same album on all providers
        new_album = await self.mass.database.async_album(item_id)
        item_provider_keys = [item["provider"] for item in new_album.provider_ids]
        for prov_id, provider in self.mass.music_manager.providers.items():
            if not prov_id in item_provider_keys:
                await provider.match_album(new_album)
        return item_id

    async def async_get_track(
        self, prov_item_id, lazy=True, track_details=None, provider=None
    ) -> Track:
        """return track details for the given provider track id"""
        if not provider:
            provider = self.id
        item_id = await self.mass.database.async_get_database_id(
            provider, prov_item_id, MediaType.Track
        )
        if item_id is None:
            # track not yet in local database so fetch details
            if not track_details:
                cache_key = f"{self.id}.get_track.{prov_item_id}"
                track_details = await cached(
                    self.cache, cache_key, self.get_track, prov_item_id
                )
            if not track_details:
                LOGGER.error("track not found: %s", prov_item_id)
                return None
            if lazy:
                asyncio.create_task(self.add_track(track_details))
                track_details.is_lazy = True
                return track_details
            item_id = await self.add_track(track_details)
        return await self.mass.database.async_track(item_id)

    async def async_add_track(self, track_details, prov_album_id=None) -> int:
        """add track to local db and return the new database id"""
        track_artists = []
        # we need to fetch track artists too
        for track_artist in track_details.artists:
            db_track_artist = await self.artist(
                track_artist.item_id,
                lazy=False,
                ref_track=track_details,
                provider=track_artist.provider,
            )
            if db_track_artist:
                track_artists.append(db_track_artist)
        track_details.artists = track_artists
        # fetch album details - prefer prov_album_id
        if prov_album_id:
            album_details = await self.album(prov_album_id, lazy=False)
            if album_details:
                track_details.album = album_details
        # make sure we have a database album
        if track_details.album and track_details.album.provider != "database":
            track_details.album = await self.album(
                track_details.album.item_id,
                lazy=False,
                provider=track_details.album.provider,
            )
        item_id = await self.mass.database.async_add_track(track_details)
        # also fetch same track on all providers (will also get other quality versions)
        new_track = await self.mass.database.async_track(item_id)
        item_provider_keys = [item["provider"] for item in new_track.provider_ids]
        for prov_id, provider in self.mass.music_manager.providers.items():
            if not prov_id in item_provider_keys:
                await provider.match_track(new_track)
        return item_id

    async def async_get_playlist(self, prov_playlist_id, provider=None) -> Playlist:
        """return playlist details for the given provider playlist id"""
        if not provider:
            provider = self.id
        db_id = await self.mass.database.async_get_database_id(
            provider, prov_playlist_id, MediaType.Playlist
        )
        if db_id is None:
            # item not yet in local database so fetch and store details
            item_details = await self.get_playlist(prov_playlist_id)
            db_id = await self.mass.database.async_add_playlist(item_details)
        return await self.mass.database.async_playlist(db_id)

    async def async_get_radio(self, prov_radio_id, provider=None) -> Radio:
        """return radio details for the given provider playlist id"""
        if not provider:
            provider = self.id
        db_id = await self.mass.database.async_get_database_id(
            provider, prov_radio_id, MediaType.Radio
        )
        if db_id is None:
            # item not yet in local database so fetch and store details
            item_details = await self.get_radio(prov_radio_id)
            db_id = await self.mass.database.async_add_radio(item_details)
        return await self.mass.database.async_radio(db_id)

    async def async_get_album_tracks(self, prov_album_id) -> List[Track]:
        """return album tracks for the given provider album id"""
        cache_key = f"{self.id}.album_tracks.{prov_album_id}"
        async for item in cached_iterator(
            self.cache, self.get_album_tracks(prov_album_id), cache_key
        ):
            if not item:
                continue
            db_id = await self.mass.database.async_get_database_id(
                item.provider, item.item_id, MediaType.Track
            )
            if db_id:
                # return database track instead if we have a match
                db_item = await self.mass.database.async_track(db_id, fulldata=False)
                db_item.disc_number = item.disc_number
                db_item.track_number = item.track_number
                yield db_item
            else:
                yield item

    async def async_get_playlist_tracks(self, prov_playlist_id) -> List[Track]:
        """return playlist tracks for the given provider playlist id"""
        playlist = await self.playlist(prov_playlist_id)
        cache_checksum = playlist.checksum
        cache_key = f"{self.id}.playlist_tracks.{prov_playlist_id}"
        pos = 0
        async for item in cached_iterator(
            self.cache,
            self.get_playlist_tracks(prov_playlist_id),
            cache_key,
            checksum=cache_checksum,
        ):
            if not item:
                continue
            db_id = await self.mass.database.async_get_database_id(
                item.provider, item.item_id, MediaType.Track
            )
            if db_id:
                # return database track instead if we have a match
                item = await self.mass.database.async_track(db_id, fulldata=False)
            item.position = pos
            pos += 1
            yield item

    async def async_get_artist_toptracks(self, artist_id: str, provider_id: str) -> List[Track]:
        """Return top tracks for an artist. Generator!"""
        if provider_id == "database":
            # albums in database
            async for item in self.mass.database.async_get_artist_toptracks(artist_id):
                yield item
        else:
            # items from provider
            provider = self.mass.get_provider(provider_id)
            cache_key = f"{provider_id}.artist_toptracks.{artist_id}"
            async for item in async_cached(
                    self.cache, cache_key, provider.async_get_artist_toptracks(artist_id)):
                if item:
                    db_id = await self.mass.database.async_get_database_id(
                        provider_id, item.item_id, MediaType.Track
                    )
                    if db_id:
                        # return database track instead if we have a match
                        yield await self.mass.database.async_get_track(db_id)
                    else:
                        yield item

    async def async_get_artist_albums(self, artist_id: str, provider_id: str) -> List[Track]:
        """Return (all) albums for an artist. Generator!"""
        if provider_id == "database":
            # albums in database
            async for item in self.mass.database.async_get_artist_albums(artist_id):
                yield item
        else:
            # items from provider
            provider = self.mass.get_provider(provider_id)
            cache_key = f"{provider_id}.artist_albums.{artist_id}"
            async for item in async_cached(
                    self.cache, cache_key, provider.async_get_artist_albums(artist_id)):
                db_id = await self.mass.database.async_get_database_id(
                    provider_id, item.item_id, MediaType.Album)
                if db_id:
                    # return database album instead if we have a match
                    yield await self.mass.database.async_get_album(db_id)
                else:
                    yield item

    async def async_match_artist(
        self, searchartist: Artist, searchalbums: List[Album], searchtracks: List[Track]
    ):
        """try to match artist in this provider by supplying db artist"""
        for searchalbum in searchalbums:
            searchstr = "%s - %s" % (searchartist.name, searchalbum.name)
            search_results = await self.search(searchstr, [MediaType.Album], limit=5)
            for strictness in [True, False]:
                for item in search_results["albums"]:
                    if item and compare_strings(
                        item.name, searchalbum.name, strict=strictness
                    ):
                        # double safety check - artist must match exactly !
                        if compare_strings(
                            item.artist.name, searchartist.name, strict=strictness
                        ):
                            # just load this item in the database where it will be strictly matched
                            await self.artist(item.artist.item_id, lazy=strictness)
                            return
        for searchtrack in searchtracks:
            searchstr = "%s - %s" % (searchartist.name, searchtrack.name)
            search_results = await self.search(searchstr, [MediaType.Track], limit=5)
            for strictness in [True, False]:
                for item in search_results["tracks"]:
                    if item and compare_strings(
                        item.name, searchtrack.name, strict=strictness
                    ):
                        # double safety check - artist must match exactly !
                        for artist in item.artists:
                            if compare_strings(
                                artist.name, searchartist.name, strict=strictness
                            ):
                                # just load this item in the database where it will be strictly matched
                                # we set skip matching to false to prevent endless recursive matching
                                await self.artist(artist.item_id, lazy=False)
                                return

    async def async_match_album(self, searchalbum: Album):
        """try to match album in this provider by supplying db album"""
        searchstr = "%s - %s" % (searchalbum.artist.name, searchalbum.name)
        if searchalbum.version:
            searchstr += " " + searchalbum.version
        search_results = await self.search(searchstr, [MediaType.Album], limit=5)
        for item in search_results["albums"]:
            if (
                item
                and (item.name in searchalbum.name or searchalbum.name in item.name)
                and compare_strings(
                    item.artist.name, searchalbum.artist.name, strict=False
                )
            ):
                # some providers mess up versions in the title, try to fix that situation
                if (
                    searchalbum.version
                    and not item.version
                    and searchalbum.name in item.name
                    and searchalbum.version in item.name
                ):
                    item.name = searchalbum.name
                    item.version = searchalbum.version
                # just load this item in the database where it will be strictly matched
                # we set skip matching to false to prevent endless recursive matching
                await self.album(item.item_id, lazy=False, album_details=item)

    async def async_match_track(self, searchtrack: Track):
        """try to match track in this provider by supplying db track"""
        searchstr = "%s - %s" % (searchtrack.artists[0].name, searchtrack.name)
        if searchtrack.version:
            searchstr += " " + searchtrack.version
        searchartists = [item.name for item in searchtrack.artists]
        search_results = await self.search(searchstr, [MediaType.Track], limit=5)
        for item in search_results["tracks"]:
            if not item or not item.name or not item.album:
                continue
            if (
                (item.name in searchtrack.name or searchtrack.name in item.name)
                and item.album
                and item.album.name == searchtrack.album.name
            ):
                # some providers mess up versions in the title, try to fix that situation
                if (
                    searchtrack.version
                    and not item.version
                    and searchtrack.name in item.name
                    and searchtrack.version in item.name
                ):
                    item.name = searchtrack.name
                    item.version = searchtrack.version
                # double safety check - artist must match exactly !
                for artist in item.artists:
                    for searchartist in searchartists:
                        if compare_strings(artist.name, searchartist, strict=False):
                            # just load this item in the database where it will be strictly matched
                            await self.track(
                                item.item_id, lazy=False, track_details=item
                            )
                            break


##########################


    async def async_get_item(
        self, item_id, media_type: MediaType, provider="database", lazy=True
    ):
        """get single music item by id and media type"""
        if media_type == MediaType.Artist:
            return await self.artist(item_id, provider, lazy=lazy)
        elif media_type == MediaType.Album:
            return await self.album(item_id, provider, lazy=lazy)
        elif media_type == MediaType.Track:
            return await self.track(item_id, provider, lazy=lazy)
        elif media_type == MediaType.Playlist:
            return await self.playlist(item_id, provider)
        elif media_type == MediaType.Radio:
            return await self.radio(item_id, provider)
        else:
            return None

    async def async_get_library_artists(
        self, orderby: str = "name", provider_filter: str = None
    ) -> List[Artist]:
        """Return all library artists, optionally filtered by provider. Iterator!"""
        async for item in self.mass.database.async_get_library_artists(
            provider_id=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_albums(self, orderby="name", provider_filter=None) -> List[Album]:
        """return all library albums, optionally filtered by provider"""
        async for item in self.mass.database.library_albums(
            provider=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_tracks(self, orderby="name", provider_filter=None) -> List[Track]:
        """return all library tracks, optionally filtered by provider"""
        async for item in self.mass.database.library_tracks(
            provider=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_playlists(
        self, orderby="name", provider_filter=None
    ) -> List[Playlist]:
        """return all library playlists, optionally filtered by provider"""
        async for item in self.mass.database.library_playlists(
            provider=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_library_radios(
        self, orderby="name", provider_filter=None
    ) -> List[Playlist]:
        """return all library radios, optionally filtered by provider"""
        async for item in self.mass.database.library_radios(
            provider=provider_filter, orderby=orderby
        ):
            yield item

    async def async_get_artist(self, item_id, provider="database", lazy=True) -> Artist:
        """get artist by id"""
        if not provider or provider == "database":
            return await self.mass.database.async_artist(item_id)
        return await self.providers[provider].artist(item_id, lazy=lazy)

    async def async_get_album(self, item_id, provider="database", lazy=True) -> Album:
        """get album by id"""
        if not provider or provider == "database":
            return await self.mass.database.async_album(item_id)
        return await self.providers[provider].album(item_id, lazy=lazy)

    async def async_get_track(
        self, item_id, provider="database", lazy=True, track_details=None
    ) -> Track:
        """get track by id"""
        if not provider or provider == "database":
            return await self.mass.database.async_track(item_id)
        return await self.providers[provider].track(
            item_id, lazy=lazy, track_details=track_details
        )

    async def async_get_playlist(self, item_id, provider="database") -> Playlist:
        """get playlist by id"""
        if not provider or provider == "database":
            return await self.mass.database.async_playlist(item_id)
        return await self.providers[provider].playlist(item_id)

    async def async_get_radio(self, item_id, provider="database") -> Radio:
        """get radio by id"""
        if not provider or provider == "database":
            return await self.mass.database.async_radio(item_id)
        return await self.providers[provider].radio(item_id)

    async def async_get_playlist_by_name(self, name) -> Playlist:
        """get playlist by name"""
        async for playlist in self.library_playlists():
            if playlist.name == name:
                return playlist
        return None

    async def async_get_radio_by_name(self, name) -> Radio:
        """get radio by name"""
        async for radio in self.library_radios():
            if radio.name == name:
                return radio
        return None

    async def async_get_album_tracks(self, album_id, provider="database") -> List[Track]:
        """get the album tracks for given album"""
        album = await self.album(album_id, provider)
        # collect the tracks from the first provider
        prov = album.provider_ids[0]
        prov_obj = self.providers[prov["provider"]]
        async for item in prov_obj.album_tracks(prov["item_id"]):
            yield item

    async def async_get_playlist_tracks(self, playlist_id, provider="database") -> List[Track]:
        """get the tracks for given playlist"""
        playlist = await self.playlist(playlist_id, provider)
        # return playlist tracks from provider
        prov = playlist.provider_ids[0]
        async for item in self.providers[prov["provider"]].playlist_tracks(
            prov["item_id"]
        ):
            yield item

    async def async_search(
        self, searchquery, media_types: List[MediaType], limit=10, online=False
    ) -> dict:
        """search database or providers"""
        # get results from database
        result = await self.mass.database.async_search(searchquery, media_types)
        if online:
            # include results from music providers
            for prov in self.providers.values():
                prov_results = await prov.search(searchquery, media_types, limit)
                for item_type, items in prov_results.items():
                    if not item_type in result:
                        result[item_type] = items
                    else:
                        result[item_type] += items
            # filter out duplicates
            for item_type, items in result.items():
                items = list(toolz.unique(items, key=operator.attrgetter("item_id")))
        return result

    async def async_library_add(self, media_items: List[MediaItem]):
        """Add media item(s) to the library"""
        result = False
        for item in media_items:
            # make sure we have a database item
            media_item = await self.item(
                item.item_id, item.media_type, item.provider, lazy=False
            )
            if not media_item:
                continue
            # add to provider's libraries
            for prov in item.provider_ids:
                prov_id = prov["provider"]
                prov_item_id = prov["item_id"]
                if prov_id in self.providers:
                    result = await self.providers[prov_id].add_library(
                        prov_item_id, media_item.media_type
                    )
                # mark as library item in internal db
                await self.mass.database.async_add_to_library(
                    media_item.item_id, media_item.media_type, prov_id
                )
        return result

    async def async_library_remove(self, media_items: List[MediaItem]):
        """Remove media item(s) from the library"""
        result = False
        for item in media_items:
            # make sure we have a database item
            media_item = await self.item(
                item.item_id, item.media_type, item.provider, lazy=False
            )
            if not media_item:
                continue
            # remove from provider's libraries
            for prov in item.provider_ids:
                prov_id = prov["provider"]
                prov_item_id = prov["item_id"]
                if prov_id in self.providers:
                    result = await self.providers[prov_id].remove_library(
                        prov_item_id, media_item.media_type
                    )
                # mark as library item in internal db
                await self.mass.database.async_remove_from_library(
                    media_item.item_id, media_item.media_type, prov_id
                )
        return result

    async def async_add_playlist_tracks(self, db_playlist_id, tracks: List[Track]):
        """add tracks to playlist - make sure we dont add dupes"""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.playlist(db_playlist_id, "database")
        if not playlist or not playlist.is_editable:
            return False
        # playlist can only have one provider (for now)
        playlist_prov = playlist.provider_ids[0]
        # grab all existing track ids in the playlist so we can check for duplicates
        cur_playlist_track_ids = []
        async for item in self.providers[playlist_prov["provider"]].playlist_tracks(
            playlist_prov["item_id"]
        ):
            cur_playlist_track_ids.append(item.item_id)
            cur_playlist_track_ids += [i["item_id"] for i in item.provider_ids]
        track_ids_to_add = []
        for track in tracks:
            # check for duplicates
            already_exists = track.item_id in cur_playlist_track_ids
            for track_prov in track.provider_ids:
                if track_prov["item_id"] in cur_playlist_track_ids:
                    already_exists = True
            if already_exists:
                continue
            # we can only add a track to a provider playlist if track is available on that provider
            # this should all be handled in the frontend but these checks are here just to be safe
            # a track can contain multiple versions on the same provider
            # simply sort by quality and just add the first one (assuming track is still available)
            for track_version in sorted(
                track.provider_ids, key=operator.itemgetter("quality"), reverse=True
            ):
                if track_version["provider"] == playlist_prov["provider"]:
                    track_ids_to_add.append(track_version["item_id"])
                    break
                elif playlist_prov["provider"] == "file":
                    # the file provider can handle uri's from all providers so simply add the uri
                    uri = f'{track_version["provider"]}://{track_version["item_id"]}'
                    track_ids_to_add.append(uri)
                    break
        # actually add the tracks to the playlist on the provider
        if track_ids_to_add:
            # invalidate cache
            await self.mass.database.async_update_playlist(
                playlist.item_id, "checksum", str(time.time())
            )
            # return result of the action on the provioer
            return await self.providers[playlist_prov["provider"]].add_playlist_tracks(
                playlist_prov["item_id"], track_ids_to_add
            )
        return False

    async def async_remove_playlist_tracks(self, db_playlist_id, tracks: List[Track]):
        """remove tracks from playlist"""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.playlist(db_playlist_id, "database")
        if not playlist or not playlist.is_editable:
            return False
        # playlist can only have one provider (for now)
        prov_playlist = playlist.provider_ids[0]
        prov_playlist_playlist_id = prov_playlist["item_id"]
        prov_playlist_id = prov_playlist["provider"]
        track_ids_to_remove = []
        for track in tracks:
            # a track can contain multiple versions on the same provider, remove all
            for track_provider in track.provider_ids:
                if track_provider["provider"] == prov_playlist_id:
                    track_ids_to_remove.append(track_provider["item_id"])
        # actually remove the tracks from the playlist on the provider
        if track_ids_to_remove:
            # invalidate cache
            await self.mass.database.async_update_playlist(
                playlist.item_id, "checksum", str(time.time())
            )
            return await self.providers[
                prov_playlist_id
            ].remove_playlist_tracks(prov_playlist_playlist_id, track_ids_to_remove)

    @run_periodic(3600 * 3)
    async def __async_sync_music_providers(self):
        """periodic sync of all music providers"""
        for prov_id in self.providers:
            self.mass.loop.create_task(self.sync_music_provider(prov_id))

    async def async_sync_music_provider(self, prov_id: str):
        """
            Sync a music provider.
            param prov_id: {string} -- provider id to sync
        """
        await self.sync_library_albums(prov_id)
        await self.sync_library_tracks(prov_id)
        await self.sync_library_artists(prov_id)
        await self.sync_library_playlists(prov_id)
        await self.sync_library_radios(prov_id)

    @sync_task("artists")
    async def async_sync_library_artists(self, prov_id):
        """sync library artists for given provider"""
        music_provider = self.providers[prov_id]
        prev_db_ids = [
            item.item_id async for item in self.library_artists(provider_filter=prov_id)
        ]
        cur_db_ids = []
        async for item in music_provider.get_library_artists():
            db_item = await music_provider.artist(item.item_id, lazy=False)
            cur_db_ids.append(db_item.item_id)
            if not db_item.item_id in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_item.item_id, MediaType.Artist, prov_id
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(db_id, MediaType.Artist, prov_id)

    @sync_task("albums")
    async def async_sync_library_albums(self, prov_id):
        """sync library albums for given provider"""
        music_provider = self.providers[prov_id]
        prev_db_ids = [
            item.item_id async for item in self.library_albums(provider_filter=prov_id)
        ]
        cur_db_ids = []
        async for item in music_provider.get_library_albums():

            db_album = await music_provider.album(
                item.item_id, album_details=item, lazy=False
            )
            if not db_album:
                LOGGER.error("provider %s album: %s", prov_id, item.__dict__)
            cur_db_ids.append(db_album.item_id)
            if not db_album.item_id in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_album.item_id, MediaType.Album, prov_id
                )
            # precache album tracks
            async for item in music_provider.album_tracks(item.item_id):
                pass
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(db_id, MediaType.Album, prov_id)

    @sync_task("tracks")
    async def async_sync_library_tracks(self, prov_id):
        """sync library tracks for given provider"""
        music_provider = self.providers[prov_id]
        prev_db_ids = [
            item.item_id async for item in self.library_tracks(provider_filter=prov_id)
        ]
        cur_db_ids = []
        async for item in music_provider.get_library_tracks():
            db_item = await music_provider.track(item.item_id, lazy=False)
            cur_db_ids.append(db_item.item_id)
            if not db_item.item_id in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_item.item_id, MediaType.Track, prov_id
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(db_id, MediaType.Track, prov_id)

    @sync_task("playlists")
    async def async_sync_library_playlists(self, prov_id):
        """sync library playlists for given provider"""
        music_provider = self.providers[prov_id]
        prev_db_ids = [
            item.item_id
            async for item in self.library_playlists(provider_filter=prov_id)
        ]
        cur_db_ids = []
        async for item in music_provider.get_library_playlists():
            # always add to db because playlist attributes could have changed
            db_id = await self.mass.database.async_add_playlist(item)
            cur_db_ids.append(db_id)
            if not db_id in prev_db_ids:
                await self.mass.database.async_add_to_library(db_id, MediaType.Playlist, prov_id)
            # precache playlist tracks
            async for item in music_provider.playlist_tracks(item.item_id):
                pass
        # process playlist deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Playlist, prov_id
                )

    @sync_task("radios")
    async def async_sync_library_radios(self, prov_id):
        """sync library radios for given provider"""
        music_provider = self.providers[prov_id]
        prev_db_ids = [
            item.item_id async for item in self.library_radios(provider_filter=prov_id)
        ]
        cur_db_ids = []
        async for item in music_provider.get_radios():
            db_id = await self.mass.database.async_get_database_id(
                prov_id, item.item_id, MediaType.Radio
            )
            if not db_id:
                db_id = await self.mass.database.async_add_radio(item)
            cur_db_ids.append(db_id)
            if not db_id in prev_db_ids:
                await self.mass.database.async_add_to_library(db_id, MediaType.Radio, prov_id)
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(db_id, MediaType.Radio, prov_id)

    async def async_get_image_thumb(self, item_id, media_type: MediaType, provider, size=50):
        """get path to (resized) thumb image for given media item"""
        cache_folder = os.path.join(self.mass.datapath, ".thumbs")
        cache_id = f"{item_id}{media_type}{provider}"
        cache_id = base64.b64encode(cache_id.encode("utf-8")).decode("utf-8")
        cache_file_org = os.path.join(cache_folder, f"{cache_id}0.png")
        cache_file_sized = os.path.join(cache_folder, f"{cache_id}{size}.png")
        if os.path.isfile(cache_file_sized):
            # return file from cache
            return cache_file_sized
        # no file in cache so we should get it
        img_url = ""
        # we only retrieve items that we already have in cache
        item = None
        if await self.mass.database.async_get_database_id(provider, item_id, media_type):
            item = await self.item(item_id, media_type, provider)
        if not item:
            return ""
        if item and item.metadata.get("image"):
            img_url = item.metadata["image"]
        elif media_type == MediaType.Track and item.album:
            # try album image instead for tracks
            return await self.get_image_thumb(
                item.album.item_id, MediaType.Album, item.album.provider, size
            )
        elif media_type == MediaType.Album and item.artist:
            # try artist image instead for albums
            return await self.get_image_thumb(
                item.artist.item_id, MediaType.Artist, item.artist.provider, size
            )
        if not img_url:
            return None
        # fetch image and store in cache
        os.makedirs(cache_folder, exist_ok=True)
        # download base image
        async with aiohttp.ClientSession() as session:
            async with session.get(img_url, verify_ssl=False) as response:
                assert response.status == 200
                img_data = await response.read()
                with open(cache_file_org, "wb") as img_file:
                    img_file.write(img_data)
        if not size:
            # return base image
            return cache_file_org
        # save resized image
        basewidth = size
        img = Image.open(cache_file_org)
        wpercent = basewidth / float(img.size[0])
        hsize = int((float(img.size[1]) * float(wpercent)))
        img = img.resize((basewidth, hsize), Image.ANTIALIAS)
        img.save(cache_file_sized)
        # return file from cache
        return cache_file_sized
