"""iTunes Podcast search support for MusicAssistant."""

from __future__ import annotations

import urllib.parse
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles
import orjson
import podcastparser
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
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
    AudioFormat,
    MediaItemImage,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.itunes_podcasts.parsers import parse_podcast, parse_podcast_episode
from music_assistant.providers.itunes_podcasts.schema import ITunesSearchResults

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


CONF_LOCALE = "locale"
CONF_EXPLICIT = "explicit"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ITunesPodcastsProvider(mass, manifest, config)


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
    json_path = Path(__file__).parent / "itunes_country_codes.json"
    async with aiofiles.open(json_path) as f:
        country_codes = orjson.loads(await f.read())

    language_options = [ConfigValueOption(val, key.lower()) for key, val in country_codes.items()]
    return (
        ConfigEntry(
            key=CONF_LOCALE,
            type=ConfigEntryType.STRING,
            label="Country",
            required=True,
            options=language_options,
            default_value="de",
        ),
        ConfigEntry(
            key=CONF_EXPLICIT,
            type=ConfigEntryType.BOOLEAN,
            label="Include explicit results",
            required=False,
            description="Whether or not to include explicit content results in search.",
            default_value=True,
        ),
    )


class ITunesPodcastsProvider(MusicProvider):
    """ITunesPodcastsProvider."""

    throttler: ThrottlerManager

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.SEARCH,
        }

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        return True

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.max_episodes = 0
        # 20 requests per minute, be a bit below
        self.throttler = ThrottlerManager(rate_limit=18, period=60)

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """Perform search on musicprovider."""
        result = SearchResults()
        if MediaType.PODCAST not in media_types:
            return result

        term = urllib.parse.quote_plus(search_query)
        if limit < 1:
            limit = 1
        elif limit > 200:
            limit = 200
        country = str(self.config.get_value(CONF_LOCALE))
        explicit = "Yes" if bool(self.config.get_value(CONF_EXPLICIT)) else "No"
        params = {
            "media": "podcast",
            "entity": "podcast",
            "country": country,
            "attribute": "titleTerm",
            "explicit": explicit,
            "limit": limit,
            "term": term,
        }
        url = f"https://itunes.apple.com/search?{urllib.parse.urlencode(params)}"
        self.logger.debug(f"Search url: {url}")
        result.podcasts = await self._perform_search(url)

        return result

    @throttle_with_retries
    async def _perform_search(self, url: str) -> list[Podcast]:
        response = await self.mass.http_session.get(
            url,
        )
        json_response = b""
        if response.status == 200:
            json_response = await response.read()
        if not json_response:
            return []
        podcast_list: list[Podcast] = []
        results = ITunesSearchResults.from_json(json_response).results
        for result in results:
            if result.feed_url is None or result.track_name is None:
                continue
            podcast = Podcast(
                name=result.track_name,
                item_id=result.feed_url,
                publisher=result.artist_name,
                provider=self.lookup_key,
                provider_mappings={
                    ProviderMapping(
                        item_id=result.feed_url,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            image_list = []
            for artwork_url in [
                result.artwork_url_600,
                result.artwork_url_100,
                result.artwork_url_60,
                result.artwork_url_30,
            ]:
                if artwork_url is not None:
                    image_list.append(
                        MediaItemImage(
                            type=ImageType.THUMB, path=artwork_url, provider=self.lookup_key
                        )
                    )
            podcast.metadata.images = UniqueList(image_list)
            podcast_list.append(podcast)
        return podcast_list

    async def _get_parsed_podcast(self, prov_podcast_id: str) -> dict[str, Any]:
        # cache this?
        # see music-assistant/server@6aae82e
        response = await self.mass.http_session.get(
            prov_podcast_id, headers={"User-Agent": "Mozilla/5.0"}
        )
        if response.status != 200:
            raise MediaNotFoundError("Podcast not found!")
        feed_data = await response.read()
        feed_stream = BytesIO(feed_data)
        # how to type hint? dict from lib
        return podcastparser.parse(  # type: ignore [no-any-return]
            prov_podcast_id,
            feed_stream,
            max_episodes=self.max_episodes,
        )

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get podcast."""
        parsed = await self._get_parsed_podcast(prov_podcast_id)

        return parse_podcast(
            feed_url=prov_podcast_id,
            parsed_feed=parsed,
            lookup_key=self.lookup_key,
            domain=self.domain,
            instance_id=self.instance_id,
        )

    async def get_podcast_episodes(self, prov_podcast_id: str) -> list[PodcastEpisode]:
        """Get podcast episodes."""
        episode_list = []
        podcast = await self._get_parsed_podcast(prov_podcast_id)
        podcast_cover = podcast.get("cover_url")
        episodes = podcast.get("episodes", [])
        for cnt, episode in enumerate(episodes):
            episode_list.append(
                parse_podcast_episode(
                    episode=episode,
                    prov_podcast_id=prov_podcast_id,
                    episode_cnt=cnt,
                    podcast_cover=podcast_cover,
                    domain=self.domain,
                    lookup_key=self.lookup_key,
                    instance_id=self.instance_id,
                )
            )
        return episode_list

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get single podcast episode."""
        prov_podcast_id, episode_id, guid = prov_episode_id.split(" ")
        podcast = await self._get_parsed_podcast(prov_podcast_id)
        podcast_cover = podcast.get("cover_url")
        episodes = podcast.get("episodes", [])
        for cnt, episode in enumerate(episodes):
            episode_enclosures = episode.get("enclosures", [])
            if len(episode_enclosures) < 1:
                raise RuntimeError
            _episode_id = episode_enclosures[0].get("url", None)
            _guid = episode.get("guid")
            if (_guid is None and episode_id == _episode_id) or guid == _guid:
                return parse_podcast_episode(
                    episode=episode,
                    prov_podcast_id=prov_podcast_id,
                    episode_cnt=cnt,
                    podcast_cover=podcast_cover,
                    domain=self.domain,
                    lookup_key=self.lookup_key,
                    instance_id=self.instance_id,
                )

        raise MediaNotFoundError("Episode not found")

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream of item."""
        _, episode_id, _ = item_id.split(" ")
        return StreamDetails(
            provider=self.lookup_key,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(episode_id),
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=episode_id,
            can_seek=True,
            allow_seek=True,
        )
