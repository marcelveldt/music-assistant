"""Extended example/script to run Music Assistant with all bells and whistles."""
import argparse
import asyncio
import logging
import os
from sys import path

from aiorun import run

# pylint: disable=wrong-import-position
from music_assistant.models.player import Player, PlayerState

path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from music_assistant.mass import MusicAssistant
from music_assistant.providers.spotify import SpotifyProvider
from music_assistant.providers.qobuz import QobuzProvider
from music_assistant.providers.tunein import TuneInProvider
from music_assistant.providers.filesystem import FileSystemProvider

parser = argparse.ArgumentParser(description="MusicAssistant")
parser.add_argument(
    "--spotify-username",
    required=False,
    help="Spotify username",
)
parser.add_argument(
    "--spotify-password",
    required=False,
    help="Spotify password.",
)
parser.add_argument(
    "--qobuz-username",
    required=False,
    help="Qobuz username",
)
parser.add_argument(
    "--qobuz-password",
    required=False,
    help="Qobuz password.",
)
parser.add_argument(
    "--tunein-username",
    required=False,
    help="Tunein username",
)
parser.add_argument(
    "--musicdir",
    required=False,
    help="Directory on disk for local music library",
)
parser.add_argument(
    "--playlistdir",
    required=False,
    help="Directory on disk for local (m3u) playlists",
)
parser.add_argument(
    "--debug",
    action="store_true",
    help="Enable verbose debug logging",
)
args = parser.parse_args()


# setup logger
logging.basicConfig(
    level=logging.DEBUG if args.debug else logging.INFO,
    format="%(asctime)-15s %(levelname)-5s %(name)s -- %(message)s",
)
# silence some loggers
logging.getLogger("aiorun").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.INFO)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("databases").setLevel(logging.WARNING)


# default database based on sqlite
data_dir = os.getenv("APPDATA") if os.name == "nt" else os.path.expanduser("~")
data_dir = os.path.join(data_dir, ".musicassistant")
if not os.path.isdir(data_dir):
    os.makedirs(data_dir)
db_file = os.path.join(data_dir, "music_assistant.db")

mass = MusicAssistant(f"sqlite:///{db_file}")


providers = []
if args.spotify_username and args.spotify_password:
    providers.append(SpotifyProvider(args.spotify_username, args.spotify_password))
if args.qobuz_username and args.qobuz_password:
    providers.append(QobuzProvider(args.qobuz_username, args.qobuz_password))
if args.tunein_username:
    providers.append(TuneInProvider(args.tunein_username))
if args.musicdir:
    providers.append(FileSystemProvider(args.musicdir, args.playlistdir))

class TestPlayer(Player):
    def __init__(self):
        self.player_id = "test"
        self.is_group = False
        self._attr_name = "Test player"
        self._attr_powered = False
        self._attr_elapsed_time = 0
        self._attr_current_url = None
        self._attr_state = PlayerState.IDLE
        self._attr_available = True
        self._attr_volume_level = 100

    async def play_url(self, url: str) -> None:
        """Play the specified url on the player."""
        print("play uri: %s" % url)
        self._attr_current_url = url
        self.update_state()

    async def stop(self) -> None:
        """Send STOP command to player."""
        print("STOP CALLED")
        self._attr_state = PlayerState.IDLE
        self._attr_current_url = None
        self._attr_elapsed_time = 0
        self.update_state()

    async def play(self) -> None:
        """Send PLAY/UNPAUSE command to player."""
        print("PLAY CALLED")
        self._attr_state = PlayerState.PLAYING
        self._attr_elapsed_time = 1
        self.update_state()

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        print("PAUSE CALLED")
        self._attr_state = PlayerState.PAUSED
        self.update_state()

    async def power(self, powered: bool) -> None:
        """Send POWER command to player."""
        print("POWER CALLED - %s" % powered)
        self._attr_powered = powered
        self._attr_current_url = None
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send volume level (0..100) command to player."""
        print("VOLUME SET CALLED - %s" % volume_level)
        self._attr_volume_level = volume_level
        self.update_state()


def main():
    """Handle main execution."""

    async def async_main():
        """Async main routine."""
        asyncio.get_event_loop().set_debug(args.debug)

        await mass.setup()
        # register music provider(s)
        for prov in providers:
            await mass.music.register_provider(prov)
        # get some data
        artists = await mass.music.artists.library()
        print(f"Got {len(artists)} artists in library")
        albums = await mass.music.albums.library()
        print(f"Got {len(albums)} albums in library")
        tracks = await mass.music.tracks.library()
        print(f"Got {len(tracks)} tracks in library")
        radios = await mass.music.radio.library()
        print(f"Got {len(radios)} radio stations in library")
        playlists = await mass.music.playlists.library()
        print(f"Got {len(playlists)} playlists in library")
        # register a player
        test_player = TestPlayer()
        await mass.players.register_player(test_player)
        # try to play some playlist
        await test_player.queue.set_crossfade_duration(10)
        await test_player.queue.set_shuffle_enabled(True)
        if len(playlists) > 0:
            await test_player.queue.play_media(playlists[0].uri)

    def on_shutdown(loop):
        loop.run_until_complete(mass.stop())

    run(
        async_main(),
        use_uvloop=True,
        shutdown_callback=on_shutdown,
        executor_workers=64,
    )


if __name__ == "__main__":
    main()
