"""Base (ABC) MediaType specific controller."""
from __future__ import annotations

import asyncio
import logging
from abc import ABCMeta, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import suppress
from time import time
from typing import TYPE_CHECKING, Generic, TypeVar

from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import EventType, MediaType, ProviderFeature
from music_assistant.common.models.errors import MediaNotFoundError
from music_assistant.common.models.media_items import (
    ItemMapping,
    MediaItemType,
    PagedItems,
    ProviderMapping,
    Track,
    media_from_dict,
)
from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS, ROOT_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant

ItemCls = TypeVar("ItemCls", bound="MediaItemType")

REFRESH_INTERVAL = 60 * 60 * 24 * 30


class MediaControllerBase(Generic[ItemCls], metaclass=ABCMeta):
    """Base model for controller managing a MediaType."""

    media_type: MediaType
    item_cls: MediaItemType
    db_table: str

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.music.{self.media_type.value}")
        self._db_add_lock = asyncio.Lock()

    @abstractmethod
    async def add(self, item: ItemCls, skip_metadata_lookup: bool = False) -> ItemCls:
        """Add item to local db and return the database item."""
        raise NotImplementedError

    async def delete(self, item_id: int, recursive: bool = False) -> None:  # noqa: ARG002
        """Delete record from the database."""
        db_item = await self.get_db_item(item_id)
        assert db_item, f"Item does not exist: {item_id}"
        # delete item
        await self.mass.music.database.delete(
            self.db_table,
            {"item_id": int(item_id)},
        )
        # update provider_mappings table
        await self.mass.music.database.delete(
            DB_TABLE_PROVIDER_MAPPINGS,
            {"media_type": self.media_type.value, "item_id": int(item_id)},
        )
        # NOTE: this does not delete any references to this item in other records,
        # this is handled/overridden in the mediatype specific controllers
        self.mass.signal_event(EventType.MEDIA_ITEM_DELETED, db_item.uri, db_item)
        self.logger.debug("deleted item with id %s from database", item_id)

    async def db_items(
        self,
        in_library: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        query_parts: list[str] | None = None,
    ) -> PagedItems:
        """Get in-database items."""
        sql_query = f"SELECT * FROM {self.db_table}"
        params = {}
        query_parts = query_parts or []
        if search:
            params["search"] = f"%{search}%"
            if self.media_type in (MediaType.ALBUM, MediaType.TRACK):
                query_parts.append("(name LIKE :search or artists LIKE :search)")
            else:
                query_parts.append("name LIKE :search")
        if in_library is not None:
            query_parts.append("in_library = :in_library")
            params["in_library"] = in_library
        if query_parts:
            sql_query += " WHERE " + " AND ".join(query_parts)
        sql_query += f" ORDER BY {order_by}"
        items = await self.get_db_items_by_query(sql_query, params, limit=limit, offset=offset)
        count = len(items)
        if 0 < count < limit:
            total = offset + count
        else:
            total = await self.mass.music.database.get_count_from_query(sql_query, params)
        return PagedItems(items, count, limit, offset, total)

    async def iter_db_items(
        self,
        in_library: bool | None = None,
        search: str | None = None,
        order_by: str = "sort_name",
    ) -> AsyncGenerator[ItemCls, None]:
        """Iterate all in-database items."""
        limit: int = 500
        offset: int = 0
        while True:
            next_items = await self.db_items(
                in_library=in_library,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            )
            for item in next_items.items:
                yield item
            if next_items.count < limit:
                break
            offset += limit

    async def get(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
        lazy: bool = True,
        details: ItemCls = None,
        add_to_db: bool = True,
    ) -> ItemCls:
        """Return (full) details for a single media item."""
        if not add_to_db and provider_instance_id_or_domain == "database":
            return await self.get_db_item(item_id)
        if details and (details.provider == "database" or isinstance(details, ItemMapping)):
            # invalidate details if not (full) provider details for this item
            details = None
        db_item = await self.get_db_item_by_prov_id(
            item_id,
            provider_instance_id_or_domain,
        )
        if db_item and (time() - (db_item.metadata.last_refresh or 0)) > REFRESH_INTERVAL:
            # it's been too long since the full metadata was last retrieved (or never at all)
            force_refresh = True
        if db_item and force_refresh and add_to_db:
            # get (first) provider item id belonging to this db item
            provider_instance_id_or_domain, item_id = await self.get_provider_mapping(db_item)
        elif db_item:
            # we have a db item and no refreshing is needed, return the results!
            return db_item
        if not details:
            # no details provider nor in db, fetch them from the provider
            details = await self.get_provider_item(
                item_id, provider_instance_id_or_domain, force_refresh=force_refresh
            )
        if not details:
            # we couldn't get a match from any of the providers, raise error
            raise MediaNotFoundError(f"Item not found: {provider_instance_id_or_domain}/{item_id}")
        if not add_to_db:
            return details
        # create task to add the item to the db, including matching metadata etc. takes some time
        # in 99% of the cases we just return lazy because we want the details as fast as possible
        # only if we really need to wait for the result (e.g. to prevent race conditions), we
        # can set lazy to false and we await the job to complete.
        task_id = f"add_{self.media_type.value}.{details.provider}.{details.item_id}"
        add_task = self.mass.create_task(self.add, details, task_id=task_id)
        if not lazy:
            await add_task
            return add_task.result()

        return details

    async def search(
        self,
        search_query: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[ItemCls]:
        """Search database or provider with given query."""
        # create safe search string
        search_query = search_query.replace("/", " ").replace("'", "")
        if provider_instance_id_or_domain == "database":
            return [
                self.item_cls.from_db_row(db_row)
                for db_row in await self.mass.music.database.search(self.db_table, search_query)
            ]
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        if ProviderFeature.SEARCH not in prov.supported_features:
            return []
        if not prov.library_supported(self.media_type):
            # assume library supported also means that this mediatype is supported
            return []

        # prefer cache items (if any)
        cache_key = f"{prov.instance_id}.search.{self.media_type.value}.{search_query}.{limit}"
        if cache := await self.mass.cache.get(cache_key):
            return [media_from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        searchresult = await prov.search(
            search_query,
            [self.media_type],
            limit,
        )
        if self.media_type == MediaType.ARTIST:
            items = searchresult.artists
        elif self.media_type == MediaType.ALBUM:
            items = searchresult.albums
        elif self.media_type == MediaType.TRACK:
            items = searchresult.tracks
        elif self.media_type == MediaType.PLAYLIST:
            items = searchresult.playlists
        else:
            items = searchresult.radio
        # store (serializable items) in cache
        if not prov.domain.startswith("filesystem"):  # do not cache filesystem results
            self.mass.create_task(
                self.mass.cache.set(cache_key, [x.to_dict() for x in items], expiration=86400 * 7)
            )
        return items

    async def add_to_library(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> None:
        """Add an item to the library."""
        prov_item = await self.get_db_item_by_prov_id(
            item_id,
            provider_instance_id_or_domain,
        )
        if prov_item is None:
            prov_item = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        if prov_item.in_library is True:
            return
        # mark as favorite/library item on provider(s)
        for prov_mapping in prov_item.provider_mappings:
            if prov := self.mass.get_provider(prov_mapping.provider_instance):
                if not prov.library_edit_supported(self.media_type):
                    continue
                await prov.library_add(prov_mapping.item_id, self.media_type)
        # mark as library item in internal db if db item
        if prov_item.provider == "database" and not prov_item.in_library:
            prov_item.in_library = True
            await self.set_db_library(prov_item.item_id, True)

    async def remove_from_library(self, item_id: str, provider_instance_id_or_domain: str) -> None:
        """Remove item from the library."""
        prov_item = await self.get_db_item_by_prov_id(
            item_id,
            provider_instance_id_or_domain,
        )
        if prov_item is None:
            prov_item = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        if prov_item.in_library is False:
            return
        # unmark as favorite/library item on provider(s)
        for prov_mapping in prov_item.provider_mappings:
            if prov := self.mass.get_provider(prov_mapping.provider_instance):
                if not prov.library_edit_supported(self.media_type):
                    continue
                await prov.library_remove(prov_mapping.item_id, self.media_type)
        # unmark as library item in internal db if db item
        if prov_item.provider == "database":
            prov_item.in_library = False
            await self.set_db_library(prov_item.item_id, False)

    async def get_provider_mapping(self, item: ItemCls) -> tuple[str, str]:
        """Return (first) provider and item id."""
        if item.provider == "database":
            # make sure we have a full object
            item = await self.get_db_item(item.item_id)
        for prefer_file in (True, False):
            for prov_mapping in item.provider_mappings:
                # returns the first provider that is available
                if not prov_mapping.available:
                    continue
                if prefer_file and not prov_mapping.provider_domain.startswith("filesystem"):
                    continue
                if self.mass.get_provider(prov_mapping.provider_instance):
                    return (prov_mapping.provider_instance, prov_mapping.item_id)
        return None, None

    async def get_db_items_by_query(
        self,
        custom_query: str | None = None,
        query_params: dict | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[ItemCls]:
        """Fetch MediaItem records from database given a custom query."""
        return [
            self.item_cls.from_db_row(db_row)
            for db_row in await self.mass.music.database.get_rows_from_query(
                custom_query, query_params, limit=limit, offset=offset
            )
        ]

    async def get_db_item(self, item_id: int | str) -> ItemCls:
        """Get record by id."""
        match = {"item_id": int(item_id)}
        if db_row := await self.mass.music.database.get_row(self.db_table, match):
            return self.item_cls.from_db_row(db_row)
        raise MediaNotFoundError(f"Album not found in database: {item_id}")

    async def get_db_item_by_prov_id(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> ItemCls | None:
        """Get the database item for the given provider_instance."""
        if provider_instance_id_or_domain == "database":
            return await self.get_db_item(item_id)
        for item in await self.get_db_items_by_prov_id(
            provider_instance_id_or_domain,
            provider_item_ids=(item_id,),
        ):
            return item
        return None

    async def get_db_items_by_prov_id(
        self,
        provider_instance_id_or_domain: str,
        provider_item_ids: tuple[str, ...] | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[ItemCls]:
        """Fetch all records from database for given provider."""
        if provider_instance_id_or_domain == "database":
            return await self.get_db_items_by_query(limit=limit, offset=offset)

        # we use the separate provider_mappings table to perform quick lookups
        # from provider id's to database id's because this is faster
        # (and more compatible) than querying the provider_mappings json column
        subquery = f"SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} "
        subquery += f"WHERE (provider_instance = '{provider_instance_id_or_domain}'"
        subquery += f" OR provider_domain = '{provider_instance_id_or_domain}')"
        if provider_item_ids is not None:
            prov_ids = str(tuple(provider_item_ids))
            if prov_ids.endswith(",)"):
                prov_ids = prov_ids.replace(",)", ")")
            subquery += f" AND provider_item_id in {prov_ids}"
        query = f"SELECT * FROM {self.db_table} WHERE item_id in ({subquery})"
        return await self.get_db_items_by_query(query, limit=limit, offset=offset)

    async def iter_db_items_by_prov_id(
        self,
        provider_instance_id_or_domain: str,
        provider_item_ids: tuple[str, ...] | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> AsyncGenerator[ItemCls, None]:
        """Iterate all records from database for given provider."""
        limit: int = 500
        offset: int = 0
        while True:
            next_items = await self.get_db_items_by_prov_id(
                provider_instance_id_or_domain,
                provider_item_ids=provider_item_ids,
                limit=limit,
                offset=offset,
            )
            for item in next_items:
                yield item
            if len(next_items) < limit:
                break
            offset += limit

    async def set_db_library(self, item_id: int, in_library: bool) -> None:
        """Set the in-library bool on a database item."""
        match = {"item_id": item_id}
        await self.mass.music.database.update(self.db_table, match, {"in_library": in_library})
        db_item = await self.get_db_item(item_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, db_item.uri, db_item)

    async def get_provider_item(
        self, item_id: str, provider_instance_id_or_domain: str, force_refresh: bool = False
    ) -> ItemCls:
        """Return item details for the given provider item id."""
        cache_key = (
            f"provider_item.{self.media_type.value}.{provider_instance_id_or_domain}.{item_id}"
        )
        if provider_instance_id_or_domain == "database":
            return await self.get_db_item(item_id)
        if not force_refresh and (cache := await self.mass.cache.get(cache_key)):
            return self.item_cls.from_dict(cache)
        if provider := self.mass.get_provider(provider_instance_id_or_domain):  # noqa: SIM102
            if item := await provider.get_item(self.media_type, item_id):
                await self.mass.cache.set(cache_key, item.to_dict())
                return item
        raise MediaNotFoundError(
            f"{self.media_type.value}://{item_id} not "
            "found on provider {provider_instance_id_or_domain}"
        )

    async def remove_prov_mapping(self, item_id: int, provider_instance_id: str) -> None:
        """Remove provider id(s) from item."""
        try:
            db_item = await self.get_db_item(item_id)
        except MediaNotFoundError:
            # edge case: already deleted / race condition
            return

        # update provider_mappings table
        await self.mass.music.database.delete(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": self.media_type.value,
                "item_id": int(item_id),
                "provider_instance": provider_instance_id,
            },
        )

        # update the item in db (provider_mappings column only)
        db_item.provider_mappings = {
            x for x in db_item.provider_mappings if x.provider_instance != provider_instance_id
        }
        match = {"item_id": item_id}
        if db_item.provider_mappings:
            await self.mass.music.database.update(
                self.db_table,
                match,
                {"provider_mappings": serialize_to_json(db_item.provider_mappings)},
            )
            self.logger.debug("removed provider %s from item id %s", provider_instance_id, item_id)
            self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, db_item.uri, db_item)
        else:
            # delete item if it has no more providers
            with suppress(AssertionError):
                await self.delete(item_id)

    async def dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[Track]:
        """Return a dynamic list of tracks based on the given item."""
        ref_item = await self.get(item_id, provider_instance_id_or_domain)
        for prov_mapping in ref_item.provider_mappings:
            prov = self.mass.get_provider(prov_mapping.provider_instance)
            if prov is None:
                continue
            if ProviderFeature.SIMILAR_TRACKS not in prov.supported_features:
                continue
            return await self._get_provider_dynamic_tracks(
                prov_mapping.item_id,
                prov_mapping.provider_instance,
                limit=limit,
            )
        # Fallback to the default implementation
        return await self._get_dynamic_tracks(ref_item)

    @abstractmethod
    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[Track]:
        """Generate a dynamic list of tracks based on the item's content."""

    @abstractmethod
    async def _get_dynamic_tracks(self, media_item: ItemCls, limit: int = 25) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""

    async def _set_provider_mappings(
        self, item_id: int, provider_mappings: list[ProviderMapping]
    ) -> None:
        """Update the provider_items table for the media item."""
        # clear all records first
        await self.mass.music.database.delete(
            DB_TABLE_PROVIDER_MAPPINGS,
            {"media_type": self.media_type.value, "item_id": int(item_id)},
        )
        # add entries
        for provider_mapping in provider_mappings:
            await self.mass.music.database.insert_or_replace(
                DB_TABLE_PROVIDER_MAPPINGS,
                {
                    "media_type": self.media_type.value,
                    "item_id": item_id,
                    "provider_domain": provider_mapping.provider_domain,
                    "provider_instance": provider_mapping.provider_instance,
                    "provider_item_id": provider_mapping.item_id,
                },
            )
