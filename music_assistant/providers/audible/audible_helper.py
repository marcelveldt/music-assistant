"""Helper for parsing and using audible api."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from collections.abc import AsyncGenerator
from os import PathLike
from typing import Any
from urllib.parse import parse_qs, urlparse

import audible
import audible.register
from audible import AsyncClient
from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.errors import LoginFailed
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    MediaItemChapter,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.mass import MusicAssistant

CACHE_DOMAIN = "audible"
CACHE_CATEGORY_API = 0
CACHE_CATEGORY_AUDIOBOOK = 1
CACHE_CATEGORY_CHAPTERS = 2


class AudibleHelper:
    """Helper for parsing and using audible api."""

    def __init__(
        self,
        mass: MusicAssistant,
        client: AsyncClient,
        provider_domain: str,
        provider_instance: str,
        logger: logging.Logger | None = None,
    ):
        """Initialize the Audible Helper."""
        self.mass = mass
        self.client = client
        self.provider_domain = provider_domain
        self.provider_instance = provider_instance
        # Ensure logger is never None by using a dummy logger if none is provided
        self.logger = logger or logging.getLogger("audible_helper")

    async def get_library(self) -> AsyncGenerator[Audiobook, None]:
        """Fetch the user's library with pagination."""
        response_groups = [
            "contributors",
            "media",
            "product_attrs",
            "product_desc",
            "product_details",
            "product_extended_attrs",
        ]

        page = 1
        page_size = 50
        total_processed = 0
        max_iterations = 100
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            self.logger.debug(
                "Audible: Fetching library page %s with page_size %s (processed so far: %s)",
                page,
                page_size,
                total_processed,
            )

            library = await self._call_api(
                "library",
                use_cache=False,
                response_groups=",".join(response_groups),
                page=page,
                num_results=page_size,
            )

            items = library.get("items", [])
            total_items = library.get("total_results", 0)

            self.logger.debug(
                "Audible: Got %s items (total reported by API: %s)", len(items), total_items
            )

            # Check if this page had no items (which means we've reached the end)
            if not items:
                self.logger.debug(
                    "Audible: No more items returned, ending pagination (processed %s items)",
                    total_processed,
                )
                break

            # Process items in this page
            items_processed_this_page = 0
            for audiobook_data in items:
                # Check if this is a podcast or non-audio asset that we should skip
                content_type = audiobook_data.get("content_delivery_type", "")
                if content_type in ("PodcastParent", "NonAudio"):
                    self.logger.debug(
                        "Skipping non-audiobook item: %s (%s)",
                        audiobook_data.get("title", "Unknown"),
                        content_type,
                    )
                    total_processed += 1
                    continue

                asin = audiobook_data.get("asin")
                cached_book = await self.mass.cache.get(
                    key=asin, base_key=CACHE_DOMAIN, category=CACHE_CATEGORY_AUDIOBOOK, default=None
                )

                if cached_book is not None:
                    album = await self._parse_audiobook(cached_book)
                    yield album
                else:
                    album = await self._parse_audiobook(audiobook_data)
                    yield album

                total_processed += 1
                items_processed_this_page += 1

            self.logger.debug(
                "Audible: Processed %s valid audiobooks on page %s", items_processed_this_page, page
            )

            # Continue to the next page
            page += 1
            self.logger.debug(
                "Audible: Moving to page %s (processed: %s, total reported: %s)",
                page,
                total_processed,
                total_items,
            )

        # Log the final results
        if iteration >= max_iterations:
            self.logger.warning(
                "Audible: Reached maximum iteration limit (%s) with %s items processed",
                max_iterations,
                total_processed,
            )
        else:
            self.logger.info(
                "Audible: Successfully retrieved %s audiobooks from library", total_processed
            )

    async def get_audiobook(self, asin: str, use_cache: bool = True) -> Audiobook | None:
        """Fetch the audiobook by asin."""
        if use_cache:
            cached_book = await self.mass.cache.get(
                key=asin, base_key=CACHE_DOMAIN, category=CACHE_CATEGORY_AUDIOBOOK, default=None
            )
            if cached_book is not None:
                return await self._parse_audiobook(cached_book)
        response = await self._call_api(
            f"library/{asin}",
            response_groups="""
                contributors, media, price, product_attrs, product_desc, product_details,
                product_extended_attrs,is_finished
                """,
        )

        if response is None:
            return None
        await self.mass.cache.set(
            key=asin,
            base_key=CACHE_DOMAIN,
            category=CACHE_CATEGORY_AUDIOBOOK,
            data=response.get("item"),
        )
        return await self._parse_audiobook(response.get("item"))

    async def get_stream(self, asin: str) -> StreamDetails:
        """Get stream details for a track (audiobook chapter)."""
        chapters = await self._fetch_chapters(asin=asin)

        duration = sum(chapter["length_ms"] for chapter in chapters) / 1000

        playback_info = await self.client.post(
            f"content/{asin}/licenserequest",
            body={
                "quality": "High",
                "response_groups": "content_reference,certificate",
                "consumption_type": "Streaming",
                "supported_media_features": {
                    "codecs": ["mp4a.40.2", "mp4a.40.42"],
                    "drm_types": [
                        "Hls",
                    ],
                },
                "spatial": False,
            },
        )
        size = (
            playback_info.get("content_license")
            .get("content_metadata")
            .get("content_reference")
            .get("content_size_in_bytes", 0)
        )

        m3u8_url = playback_info.get("content_license").get("license_response")
        acr = playback_info.get("content_license").get("acr")
        return StreamDetails(
            provider=self.provider_instance,
            size=size,
            item_id=f"{asin}",
            audio_format=AudioFormat(content_type=ContentType.AAC),
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.HTTP,
            path=m3u8_url,
            can_seek=True,
            allow_seek=True,
            duration=duration,
            data={"acr": acr},
        )

    async def _fetch_chapters(self, asin: str) -> list[dict[str, Any]]:
        """Fetch chapter data for an audiobook."""
        if not asin or asin == "error":
            self.logger.warning(
                "Invalid ASIN provided to _fetch_chapters, returning empty chapter list"
            )
            return []

        chapters_data: list[Any] = await self.mass.cache.get(
            base_key=CACHE_DOMAIN, category=CACHE_CATEGORY_CHAPTERS, key=asin, default=[]
        )

        if not chapters_data:
            try:
                response = await self._call_api(
                    f"content/{asin}/metadata",
                    response_groups="chapter_info, always-returned, content_reference, content_url",
                    chapter_titles_type="Flat",
                )

                if not response:
                    self.logger.warning(f"Failed to get metadata for ASIN {asin}")
                    return []

                content_metadata = response.get("content_metadata")
                if not content_metadata:
                    self.logger.warning(f"No content_metadata for ASIN {asin}")
                    return []

                chapter_info = content_metadata.get("chapter_info")
                if not chapter_info:
                    self.logger.warning(f"No chapter_info for ASIN {asin}")
                    return []

                chapters_data = chapter_info.get("chapters", [])

                await self.mass.cache.set(
                    base_key=CACHE_DOMAIN,
                    category=CACHE_CATEGORY_CHAPTERS,
                    key=asin,
                    data=chapters_data,
                )
            except Exception as exc:
                self.logger.error(f"Error fetching chapters for ASIN {asin}: {exc}")
                chapters_data = []

        return chapters_data

    async def get_last_postion(self, asin: str) -> int:
        """Fetch last position of asin."""
        if not asin or asin == "error":
            return 0

        try:
            response = await self._call_api("annotations/lastpositions", asins=asin)

            if not response:
                self.logger.debug(f"No last position data available for ASIN {asin}")
                return 0

            annotations = response.get("asin_last_position_heard_annots")
            if not annotations or not isinstance(annotations, list) or len(annotations) == 0:
                self.logger.debug(f"No annotations found for ASIN {asin}")
                return 0

            annotation = annotations[0]
            if not annotation or not isinstance(annotation, dict):
                self.logger.debug(f"Invalid annotation for ASIN {asin}")
                return 0

            last_position = annotation.get("last_position_heard")
            if not last_position or not isinstance(last_position, dict):
                self.logger.debug(f"Invalid last_position for ASIN {asin}")
                return 0

            position_ms = last_position.get("position_ms", 0)
            return int(position_ms)

        except Exception as exc:
            self.logger.error(f"Error getting last position for ASIN {asin}: {exc}")
            return 0

    async def set_last_position(self, asin: str, pos: int) -> None:
        """Report last position."""
        # TODO: Implement position reporting

    async def _call_api(self, path: str, **kwargs: Any) -> Any:
        response = None
        use_cache = False
        params_str = json.dumps(kwargs, sort_keys=True)
        params_hash = hashlib.md5(params_str.encode()).hexdigest()
        cache_key_with_params = f"{path}:{params_hash}"
        if use_cache:
            response = await self.mass.cache.get(
                key=cache_key_with_params, base_key=CACHE_DOMAIN, category=CACHE_CATEGORY_API
            )
        if not response:
            response = await self.client.get(path, **kwargs)
            await self.mass.cache.set(
                key=cache_key_with_params, base_key=CACHE_DOMAIN, data=response
            )
        return response

    async def _parse_audiobook(self, audiobook_data: dict[str, Any] | None) -> Audiobook:
        if audiobook_data is None:
            self.logger.error("Received None audiobook_data in _parse_audiobook")
            # Return a minimal valid Audiobook to avoid breaking the iteration
            return Audiobook(
                item_id="error",
                provider=self.provider_instance,
                name="Error: Missing Audiobook Data",
                duration=0,
                provider_mappings={
                    ProviderMapping(
                        item_id="error",
                        provider_domain=self.provider_domain,
                        provider_instance=self.provider_instance,
                    )
                },
            )

        asin = audiobook_data.get("asin", "")
        title = audiobook_data.get("title", "")
        authors = []
        narrators = []

        # Safely handle narrators
        narrators_list = audiobook_data.get("narrators") or []
        if isinstance(narrators_list, list):
            for narrator in narrators_list:
                if narrator and isinstance(narrator, dict):
                    narrators.append(narrator.get("name", "Unknown Narrator"))

        # Safely handle authors
        authors_list = audiobook_data.get("authors") or []
        if isinstance(authors_list, list):
            for author in authors_list:
                if author and isinstance(author, dict):
                    authors.append(author.get("name", "Unknown Author"))
        chapters_data = await self._fetch_chapters(asin=asin)
        duration = sum(chapter["length_ms"] for chapter in chapters_data) / 1000
        book = Audiobook(
            item_id=asin,
            provider=self.provider_instance,
            name=title,
            duration=duration,
            provider_mappings={
                ProviderMapping(
                    item_id=asin,
                    provider_domain=self.provider_domain,
                    provider_instance=self.provider_instance,
                )
            },
            publisher=audiobook_data.get("publisher_name"),
            authors=UniqueList(authors),
            narrators=UniqueList(narrators),
        )
        book.metadata.copyright = audiobook_data.get("copyright")
        book.metadata.description = _html_to_txt(
            str(audiobook_data.get("extended_product_description", ""))
        )
        book.metadata.languages = UniqueList([audiobook_data.get("language", "")])
        book.metadata.release_date = audiobook_data.get("release_date")
        reviews = audiobook_data.get("editorial_reviews", [])
        if reviews:
            book.metadata.review = _html_to_txt(reviews[0])
        book.metadata.genres = {
            genre.replace("_", " ") for genre in audiobook_data.get("platinum_keywords", "")
        }
        book.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=audiobook_data.get("product_images", {}).get("500"),
                    provider=self.provider_instance,
                    remotely_accessible=True,
                ),
                MediaItemImage(
                    type=ImageType.CLEARART,
                    path=audiobook_data.get("product_images", {}).get("500"),
                    provider=self.provider_instance,
                    remotely_accessible=True,
                ),
            ]
        )

        chapters = []
        for index, chapter_data in enumerate(chapters_data):
            start = int(chapter_data.get("start_offset_sec", 0))
            length = int(chapter_data.get("length_ms", 0)) / 1000
            # Get chapter title with fallback and explicit typing
            raw_title = chapter_data.get("title")
            chapter_title: str
            if raw_title is None:
                chapter_title = f"Chapter {index + 1}"
            elif isinstance(raw_title, str):
                chapter_title = raw_title
            else:
                chapter_title = str(raw_title)

            chapters.append(
                MediaItemChapter(
                    position=index, name=chapter_title, start=start, end=start + length
                )
            )
        book.metadata.chapters = chapters
        book.resume_position_ms = await self.get_last_postion(asin=asin)
        return book

    async def deregister(self) -> None:
        """Deregister this provider from Audible."""
        await asyncio.to_thread(self.client.auth.deregister_device)


def _html_to_txt(html_text: str) -> str:
    txt = html.unescape(html_text)
    tags = re.findall("<[^>]+>", txt)
    for tag in tags:
        txt = txt.replace(tag, "")
    return txt


# Audible Authorization
async def audible_get_auth_info(locale: str) -> tuple[str, str, str]:
    """
    Generate the login URL and auth info for Audible OAuth flow asynchronously.

    Args:
        locale: The locale string (e.g., 'us', 'uk', 'de') to determine region settings
    Returns:
        A tuple containing:
        - code_verifier (str): The OAuth code verifier string
        - oauth_url (str): The complete OAuth URL for login
        - serial (str): The generated device serial number
    """
    # Create locale object (not I/O operation)
    locale_obj = audible.localization.Locale(locale)

    # Create code verifier (potential crypto operations)
    code_verifier = await asyncio.to_thread(audible.login.create_code_verifier)

    # Build OAuth URL (potential network operations)
    oauth_url, serial = await asyncio.to_thread(
        audible.login.build_oauth_url,
        country_code=locale_obj.country_code,
        domain=locale_obj.domain,
        market_place_id=locale_obj.market_place_id,
        code_verifier=code_verifier,
        with_username=False,
    )

    return code_verifier.decode(), oauth_url, serial


async def audible_custom_login(
    code_verifier: str, response_url: str, serial: str, locale: str
) -> audible.Authenticator:
    """
    Complete the authentication using the code_verifier, response_url, and serial asynchronously.

    Args:
        code_verifier: The code verifier string used in OAuth flow
        response_url: The response URL containing the authorization code
        serial: The device serial number
        locale: The locale string
    Returns:
        Audible Authenticator object
    Raises:
        LoginFailed: If authorization code is not found in the URL
    """
    auth = audible.Authenticator()
    auth.locale = audible.localization.Locale(locale)

    # URL parsing (not I/O operation)
    response_url_parsed = urlparse(response_url)
    parsed_qs = parse_qs(response_url_parsed.query)

    authorization_codes = parsed_qs.get("openid.oa2.authorization_code")
    if not authorization_codes:
        raise LoginFailed("Authorization code not found in the provided URL.")

    # Get the first authorization code from the list
    authorization_code = authorization_codes[0]

    # Register device (network operation)
    registration_data = await asyncio.to_thread(
        audible.register.register,
        authorization_code=authorization_code,
        code_verifier=code_verifier.encode(),
        domain=auth.locale.domain,
        serial=serial,
    )
    auth._update_attrs(**registration_data)
    return auth


async def check_file_exists(path: str | PathLike[str]) -> bool:
    """Async file exists check."""
    return await asyncio.to_thread(os.path.exists, path)


async def remove_file(path: str | PathLike[str]) -> None:
    """Async file delete."""
    await asyncio.to_thread(os.remove, path)
