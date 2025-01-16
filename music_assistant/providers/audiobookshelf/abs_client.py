"""Simple Client for Audiobookshelf.

We only implement the functions necessary for mass.
"""

from collections.abc import AsyncGenerator
from enum import Enum
from typing import Any

from aiohttp import ClientSession

from music_assistant.providers.audiobookshelf.abs_schema import (
    ABSAudioBook,
    ABSLibrariesItemsResponse,
    ABSLibrariesResponse,
    ABSLibrary,
    ABSLibraryItem,
    ABSLoginResponse,
    ABSMediaProgress,
    ABSPodcast,
    ABSUser,
)

# use page calls in case of large libraries
LIMIT_ITEMS_PER_PAGE = 10


class ABSStatus(Enum):
    """ABS Status Enum."""

    STATUS_OK = 200
    STATUS_NOT_FOUND = 404


class ABSClient:
    """Simple Audiobookshelf client.

    Only implements methods needed for Music Assistant.
    """

    def __init__(self) -> None:
        """Client authorization."""
        self.podcast_libraries: list[ABSLibrary] = []
        self.audiobook_libraries: list[ABSLibrary] = []
        self.user: ABSUser
        self.check_ssl: bool

    async def init(
        self,
        session: ClientSession,
        base_url: str,
        username: str,
        password: str,
        check_ssl: bool = True,
    ) -> None:
        """Initialize."""
        self.session = session
        self.base_url = base_url
        self.check_ssl = check_ssl
        self.session_headers = {}
        self.user = await self.login(username=username, password=password)
        self.token: str = self.user.token
        self.session_headers = {"Authorization": f"Bearer {self.token}"}

    async def _post(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
        add_api_endpoint: bool = True,
    ) -> bytes:
        """POST request to abs api.

        login and logout endpoint do not have "api" in url
        """
        _endpoint = (
            f"{self.base_url}/api/{endpoint}" if add_api_endpoint else f"{self.base_url}/{endpoint}"
        )
        response = await self.session.post(
            _endpoint, json=data, ssl=self.check_ssl, headers=self.session_headers
        )
        status = response.status
        if status != ABSStatus.STATUS_OK.value:
            raise RuntimeError(f"API post call to {endpoint=} failed.")
        return await response.read()

    async def _get(self, endpoint: str, params: dict[str, str | int] | None = None) -> bytes:
        """GET request to abs api."""
        _endpoint = f"{self.base_url}/api/{endpoint}"
        response = await self.session.get(
            _endpoint, params=params, ssl=self.check_ssl, headers=self.session_headers
        )
        status = response.status
        if status not in [ABSStatus.STATUS_OK.value, ABSStatus.STATUS_NOT_FOUND.value]:
            raise RuntimeError(f"API get call to {endpoint=} failed.")
        if response.content_type == "application/json":
            return await response.read()
        elif status == ABSStatus.STATUS_NOT_FOUND.value:
            return b""
        else:
            raise RuntimeError("Response must be json.")

    async def _patch(self, endpoint: str, data: dict[str, Any] | None = None) -> None:
        """PATCH request to abs api."""
        _endpoint = f"{self.base_url}/api/{endpoint}"
        response = await self.session.patch(
            _endpoint, json=data, ssl=self.check_ssl, headers=self.session_headers
        )
        status = response.status
        if status != ABSStatus.STATUS_OK.value:
            raise RuntimeError(f"API patch call to {endpoint=} failed.")

    async def login(self, username: str, password: str) -> ABSUser:
        """Obtain user holding token from ABS with username/ password authentication."""
        data = await self._post(
            "login",
            add_api_endpoint=False,
            data={"username": username, "password": password},
        )

        return ABSLoginResponse.from_json(data).user

    async def logout(self) -> None:
        """Logout from ABS."""
        await self._post("logout", add_api_endpoint=False)

    async def get_user(self, id_: str) -> ABSUser:
        """Get an ABS user."""
        data = await self._get(f"users/{id_}")
        return ABSUser.from_json(data)

    async def sync(self) -> None:
        """Update available book and podcast libraries."""
        data = await self._get("libraries")
        libraries = ABSLibrariesResponse.from_json(data)
        ids = [x.id_ for x in self.audiobook_libraries]
        ids.extend([x.id_ for x in self.podcast_libraries])
        for library in libraries.libraries:
            media_type = library.media_type
            if library.id_ not in ids:
                if media_type == "book":
                    self.audiobook_libraries.append(library)
                elif media_type == "podcast":
                    self.podcast_libraries.append(library)
        self.user = await self.get_user(self.user.id_)

    async def get_all_podcasts(self) -> AsyncGenerator[ABSPodcast]:
        """Get all available podcasts."""
        for library in self.podcast_libraries:
            async for podcast in self.get_all_podcasts_by_library(library):
                yield podcast

    async def _get_lib_items(self, lib: ABSLibrary) -> AsyncGenerator[bytes]:
        """Get library items with pagination."""
        page_cnt = 0
        while True:
            data = await self._get(
                f"/libraries/{lib.id_}/items",
                params={"limit": LIMIT_ITEMS_PER_PAGE, "page": page_cnt},
            )
            page_cnt += 1
            yield data

    async def get_all_podcasts_by_library(self, lib: ABSLibrary) -> AsyncGenerator[ABSPodcast]:
        """Get all podcasts in a library."""
        async for podcast_data in self._get_lib_items(lib):
            podcast_list = ABSLibrariesItemsResponse.from_json(podcast_data).results
            if not podcast_list:  # [] if page exceeds
                return

            async def _get_id(plist: list[ABSLibraryItem] = podcast_list) -> AsyncGenerator[str]:
                for entry in plist:
                    yield entry.id_

            async for id_ in _get_id():
                podcast = await self.get_podcast(id_)
                yield podcast

    async def get_podcast(self, id_: str) -> ABSPodcast:
        """Get a single Podcast by ID."""
        # this endpoint gives more podcast extra data
        data = await self._get(f"items/{id_}?expanded=1")
        return ABSPodcast.from_json(data)

    async def _get_progress_ms(
        self,
        endpoint: str,
    ) -> tuple[int | None, bool]:
        data = await self._get(endpoint=endpoint)
        if not data:
            # entry doesn't exist, so it wasn't played yet
            return 0, False
        abs_media_progress = ABSMediaProgress.from_json(data)

        return (
            int(abs_media_progress.current_time * 1000),
            abs_media_progress.is_finished,
        )

    async def get_podcast_progress_ms(
        self, podcast_id: str, episode_id: str
    ) -> tuple[int | None, bool]:
        """Get podcast progress."""
        endpoint = f"me/progress/{podcast_id}/{episode_id}"
        return await self._get_progress_ms(endpoint)

    async def get_audiobook_progress_ms(self, audiobook_id: str) -> tuple[int | None, bool]:
        """Get audiobook progress."""
        endpoint = f"me/progress/{audiobook_id}"
        return await self._get_progress_ms(endpoint)

    async def _update_progress(
        self,
        endpoint: str,
        progress_seconds: int,
        duration_seconds: int,
        is_finished: bool,
    ) -> None:
        """Update progress of media item.

        0 <= progress_percent <= 1

        Notes:
            - progress in abs is percentage
            - multiple parameters in one call don't work in all combinations
            - currentTime is current position in s
            - currentTime works only if duration is sent as well, but then don't
              send progress at the same time.
        """
        await self._patch(
            endpoint,
            data={"isFinished": is_finished},
        )
        if is_finished:
            return
        await self._patch(
            endpoint,
            data={"progress": progress_seconds / duration_seconds},
        )
        await self._patch(
            endpoint,
            data={"duration": duration_seconds, "currentTime": progress_seconds},
        )

    async def update_podcast_progress(
        self,
        podcast_id: str,
        episode_id: str,
        progress_s: int,
        duration_s: int,
        is_finished: bool = False,
    ) -> None:
        """Update podcast episode progress."""
        endpoint = f"me/progress/{podcast_id}/{episode_id}"

        await self._update_progress(endpoint, progress_s, duration_s, is_finished)

    async def update_audiobook_progress(
        self,
        audiobook_id: str,
        progress_s: int,
        duration_s: int,
        is_finished: bool = False,
    ) -> None:
        """Update audiobook progress."""
        endpoint = f"me/progress/{audiobook_id}"
        await self._update_progress(endpoint, progress_s, duration_s, is_finished)

    async def get_all_audiobooks(self) -> AsyncGenerator[ABSAudioBook]:
        """Get all audiobooks."""
        for library in self.audiobook_libraries:
            async for book in self.get_all_audiobooks_by_library(library):
                yield book

    async def get_all_audiobooks_by_library(self, lib: ABSLibrary) -> AsyncGenerator[ABSAudioBook]:
        """Get all Audiobooks in a library."""
        async for audiobook_data in self._get_lib_items(lib):
            audiobook_list = ABSLibrariesItemsResponse.from_json(audiobook_data).results
            if not audiobook_list:  # [] if page exceeds
                return

            async def _get_id(alist: list[ABSLibraryItem] = audiobook_list) -> AsyncGenerator[str]:
                for entry in alist:
                    yield entry.id_

            async for id_ in _get_id():
                audiobook = await self.get_audiobook(id_)
                yield audiobook

    async def get_audiobook(self, id_: str) -> ABSAudioBook:
        """Get a single Audiobook by ID."""
        # this endpoint gives more audiobook extra data
        audiobook = await self._get(f"items/{id_}?expanded=1")
        return ABSAudioBook.from_json(audiobook)
