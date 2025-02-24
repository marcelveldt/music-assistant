"""Schema for iTunes Podcast Search.

Only what is needed.
"""

from dataclasses import dataclass, field
from typing import Annotated

from mashumaro.config import BaseConfig
from mashumaro.mixins.json import DataClassJSONMixin
from mashumaro.types import Alias


class _BaseModel(DataClassJSONMixin):
    """Model shared between schema definitions."""

    class Config(BaseConfig):
        """Base configuration."""

        forbid_extra_keys = False
        serialize_by_alias = True


@dataclass(kw_only=True)
class PodcastSearchResult(_BaseModel):
    """PodcastSearchResult."""

    kind: str | None = None
    artist_name: Annotated[str | None, Alias("artistName")] = None
    collection_name: Annotated[str | None, Alias("collectionName")] = None
    collection_censored_name: Annotated[str | None, Alias("collectionCensoredName")] = None
    track_name: Annotated[str | None, Alias("trackName")] = None
    track_censored_name: Annotated[str | None, Alias("trackCensoredName")] = None
    feed_url: Annotated[str | None, Alias("feedUrl")] = None
    artwork_url_30: Annotated[str | None, Alias("artworkUrl30")] = None
    artwork_url_60: Annotated[str | None, Alias("artworkUrl60")] = None
    artwork_url_100: Annotated[str | None, Alias("artworkUrl100")] = None
    artwork_url_600: Annotated[str | None, Alias("artworkUrl600")] = None
    release_date: Annotated[str | None, Alias("releaseDate")] = None
    track_count: Annotated[int, Alias("trackCount")] = 0
    primary_genre_name: Annotated[str | None, Alias("primaryGenreName")] = None
    genres: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class ITunesSearchResults(_BaseModel):
    """SearchResults."""

    result_count: Annotated[int, Alias("resultCount")] = 0
    results: list[PodcastSearchResult] = field(default_factory=list)
