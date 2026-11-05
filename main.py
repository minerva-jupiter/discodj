import discord
import yt_dlp
import config
import asyncio
import random
from collections import defaultdict, deque

YDL_OPTIONS = {'format': 'bestaudio'}
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

class DiscoDJ:
    def __init__(self, client):
        self.client = client
        self.queues = defaultdict(deque)
        self.dj_modes = defaultdict(bool)
        self.current_tracks = {}
        self.prefetch_tasks = {}
        self.history = defaultdict(lambda: deque(maxlen=20))

    def _get_video_id(self, url):
        if not url: return None
        if 'watch?v=' in url: return url.split('watch?v=')[1].split('&')[0]
        if 'youtu.be/' in url: return url.split('youtu.be/')[1].split('?')[0]
        return url

    async def _ensure_voice_client(self, message):
        if not message.author.voice or not message.author.voice.channel:
            print('[Command] User is not in a voice channel.')
            return None

        voice_channel = message.author.voice.channel
        vc = message.guild.voice_client

        if vc is None:
            vc = await voice_channel.connect(self_deaf=True)
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
        
        return vc

    async def fetch_track_info(self, url, ydl_options=YDL_OPTIONS):
        def extract():
            with yt_dlp.YoutubeDL(ydl_options) as ydl:
                return ydl.extract_info(url, download=False)
        return await asyncio.to_thread(extract)

    async def fetch_random_track(self):
        seed_queries = [
            "ytsearch1:lofi hip hop radio",
            "ytsearch1:ambient electronic music",
            "ytsearch1:vocaloid",
            "ytsearch1:indie rock",
            "ytsearch1:jazz"
        ]
        chosen_query = random.choice(seed_queries)
        print(f'[Command] Searching for a random starting point ({chosen_query})...')

        search_opts = {'format': 'bestaudio', 'noplaylist': True}
        search_info = await self.fetch_track_info(chosen_query, search_opts)
        entries = search_info.get('entries', [])
        if entries:
            entry = entries[0]
            actual_url = entry.get('webpage_url') or entry.get('url')
            return {
                'url': entry['url'],
                'title': entry.get('title', 'Unknown Title'),
                'original_url': actual_url
            }
        raise Exception("Initial audio source not found.")

    async def fetch_recommended_track(self, current_track, guild_id):
        current_original_url = current_track.get('original_url')
        current_title = current_track.get('title')
        current_vid = current_track.get('id') or self._get_video_id(current_original_url)

        ydl_opts = {
            'format': 'bestaudio',
            'extract_flat': True,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
        }

        def blocking_fetch():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(current_original_url, download=False)
                except Exception:
                    return None

                suggestions = info.get('related_videos') or info.get('entries') or info.get('related')
                if not suggestions:
                    return None

                valid_candidates = []
                for s in suggestions:
                    if not s: continue
                    v_id = s.get('id') or self._get_video_id(s.get('url'))
                    if not v_id or v_id == current_vid or v_id in self.history[guild_id]:
                        continue
                    valid_candidates.append(s)

                if not valid_candidates:
                    return None
                
                chosen = random.choice(valid_candidates[:3])
                next_url = chosen.get('url') or f"https://www.youtube.com/watch?v={chosen.get('id')}"

                with yt_dlp.YoutubeDL(YDL_OPTIONS) as stream_ydl:
                    stream_info = stream_ydl.extract_info(next_url, download=False)
                    return {
                        'id': stream_info.get('id'),
                        'url': stream_info['url'],
                        'title': stream_info.get('title', 'Unknown Title'),
                        'original_url': next_url
                    }

        try:
            res = await asyncio.to_thread(blocking_fetch)
            if res:
                return res
            
            print(f'[DJ] "Up Next" data missing for "{current_title}". Using similarity search...')
            search_query = f"ytsearch5:{current_title} official audio"
            search_opts = {'format': 'bestaudio', 'noplaylist': True, 'quiet': True}
            search_info = await self.fetch_track_info(search_query, search_opts)
            
            for entry in search_info.get('entries', []):
                v_id = entry.get('id')
                if v_id and v_id != current_vid and v_id not in self.history[guild_id]:
                    return {
                        'id': v_id,
                        'url': entry['url'],
                        'title': entry.get('title', 'Unknown Title'),
                        'original_url': entry.get('webpage_url') or entry.get('url')
                    }
            
            return await self.fetch_random_track()

        except Exception as e:
            print(f'[Error] Recommendation fetch error: {e}')
            return None

    async def _prefetch_next(self, guild_id, current_track):
        print(f'[DJ] Prefetching for guild {guild_id} based on "{current_track.get("title")}"')
        
        v_id = current_track.get('id')
        if v_id:
            self.history[guild_id].append(v_id)

        next_track = await self.fetch_recommended_track(current_track, guild_id)
        
        if next_track:
            if self.dj_modes[guild_id]:
                self.queues[guild_id].append(next_track)
                print(f'[DJ] Added to queue: {next_track["title"]}')
            else:
                print(f'[DJ] Prefetch discarded (DJ mode off).')
        else:
            print(f'[DJ] Could not find any suitable next track.')

    async def play_next(self, guild_id, vc, channel):
        if not self.queues[guild_id] and self.dj_modes[guild_id]:
            if guild_id in self.prefetch_tasks and not self.prefetch_tasks[guild_id].done():
                print('[Player] Queue empty, waiting for active prefetch...')
                try:
                    await asyncio.wait_for(asyncio.shield(self.prefetch_tasks[guild_id]), timeout=15.0)
                except Exception as e:
                    print(f'[Player] Wait for prefetch timed out: {e}')

        if not self.queues[guild_id]:
            print(f'[Player] Queue is empty for {guild_id}. Ending session.')
            self.dj_modes[guild_id] = False
            if guild_id in self.current_tracks:
                del self.current_tracks[guild_id]
            if vc and vc.is_connected():
                await vc.disconnect()
            return

        track = self.queues[guild_id].popleft()
        self.current_tracks[guild_id] = track

        if self.dj_modes[guild_id] and 'original_url' in track:
            if guild_id in self.prefetch_tasks and not self.prefetch_tasks[guild_id].done():
                self.prefetch_tasks[guild_id].cancel()

            self.prefetch_tasks[guild_id] = asyncio.create_task(self._prefetch_next(guild_id, track))

        def after_playing(e):
            if e:
                print(f'[Player error] Playback stopped with error: {e}')
            
            # Use the client's event loop to schedule the next track
            if self.client.is_closed():
                return

            future = asyncio.run_coroutine_threadsafe(
                self.play_next(guild_id, vc, channel), 
                self.client.loop
            )
            try:
                future.result(timeout=1) # Just check if scheduling succeeded
            except Exception:
                pass

        try:
            # Re-verify voice client state before playing
            if not vc or not vc.is_connected():
                print(f'[Play error] Voice client disconnected before starting {track["title"]}')
                return

            source = discord.FFmpegPCMAudio(track['url'], **FFMPEG_OPTIONS)
            vc.play(source, after=after_playing)
            print(f'[Playing] {track["title"]}')
        except Exception as e:
            print(f'[Play error] {e}')
            asyncio.run_coroutine_threadsafe(self.play_next(guild_id, vc, channel), self.client.loop)

    async def handle_command(self, message):
        parts = message.content.strip().split()
        subcommand = parts[1]
        guild_id = message.guild.id
        vc = message.guild.voice_client

        if subcommand == 'pause':
            if vc and vc.is_playing():
                vc.pause()
                print('[Command] Paused.')
            elif vc and vc.is_paused():
                vc.resume()
                print('[Command] Resumed playback.')
            else:
                print('[Command] Not currently playing.')
            return

        if subcommand == 'stop':
            self.dj_modes[guild_id] = False
            if guild_id in self.queues: self.queues[guild_id].clear()
            if guild_id in self.current_tracks: del self.current_tracks[guild_id]
            if guild_id in self.prefetch_tasks:
                self.prefetch_tasks[guild_id].cancel()
                del self.prefetch_tasks[guild_id]

            if vc and (vc.is_playing() or vc.is_paused()):
                vc.stop()
                print('[Command] Stopped and cleared queue.')
            elif vc:
                await vc.disconnect()
            return

        if subcommand == 'next':
            if vc and (vc.is_playing() or vc.is_paused()):
                vc.stop()
                print('[Command] Skipping to the next track.')
            else:
                print('[Command] No track currently playing.')
            return

        if subcommand == 'dj':
            self.dj_modes[guild_id] = True
            print('[Command] Auto DJ mode enabled.')

            vc = await self._ensure_voice_client(message)
            if not vc: return

            initial_url = parts[2] if len(parts) > 2 else None
            try:
                if initial_url:
                    print('[Command] Fetching track info for starting point...')
                    info = await self.fetch_track_info(initial_url)
                    entry = info['entries'][0] if 'entries' in info and info['entries'] else info
                    track = {
                        'url': entry['url'],
                        'title': entry.get('title', 'Unknown Title'),
                        'original_url': initial_url
                    }
                else:
                    track = await self.fetch_random_track()
                
                self.queues[guild_id].append(track)
            except Exception as e:
                print(f'[Error] DJ initialization failed: {e}')
                return

            if not vc.is_playing() and not vc.is_paused():
                await self.play_next(guild_id, vc, message.channel)
            return

        # Case: 'dj [url]' or just adding tracks
        url = subcommand
        vc = await self._ensure_voice_client(message)
        if not vc: return

        print('[Command] Fetching track info...')
        try:
            info = await self.fetch_track_info(url)
            added = []
            if 'entries' in info and info['entries']:
                for entry in [e for e in info['entries'] if e]:
                    added.append({
                        'url': entry['url'],
                        'title': entry.get('title', 'Unknown Title'),
                        'original_url': url
                    })
            else:
                added.append({
                    'url': info['url'],
                    'title': info.get('title', 'Unknown Title'),
                    'original_url': url
                })

            for track in added: self.queues[guild_id].append(track)
            print(f'[Command] Added {len(added)} track(s) to the queue.')

            if not vc.is_playing() and not vc.is_paused():
                await self.play_next(guild_id, vc, message.channel)
        except Exception as e:
            print(f'[Error] Processing failed: {e}')

class BotClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.dj = DiscoDJ(self)

    async def on_ready(self):
        print(f'Logged in as {self.user}')

    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return

        content = message.content.strip()
        if not content.startswith('dj'):
            return

        parts = content.split()
        if len(parts) < 2:
            print('[Command] Not enough arguments.')
            return

        await self.dj.handle_command(message)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

client = BotClient(intents=intents)
client.run(config.DISCORD_TOKEN)
