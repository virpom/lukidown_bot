"""VK Music downloader module interacting with VK API method endpoints."""

import json
import logging
import re
from pathlib import Path

import httpx

from config import config
from downloaders.collections import _process_collection_tracks
from downloaders.core import (
    AUDIO_FORMATS,
    CancelCheck,
    DownloadCancelled,
    DownloadResult,
    FileTooLarge,
    ProgressCallback,
    _download_track_search,
    _embed_cover_mp3,
    _human_size,
    _safe_filename,
    download_ytdlp,
)
from downloaders.http import get_http_client

log = logging.getLogger("mediabot.vk_music")

VK_API_URL = "https://api.vk.com/method"
VK_CLIENT_ID = "6287487"
VK_CLIENT_SECRET = "QbYic1K3lEV5kTGiqlq2"
VK_API_VERSION = "5.282"
VK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


class ProfileClosedError(RuntimeError):
    """Raised when a VK profile's audio is inaccessible (private, non-user, or no token)."""


async def _vk_api_request(url: str, params: dict) -> dict:
    """Perform HTTP POST request to VK API and return parsed JSON."""
    client = await get_http_client(enable_proxy=True)
    headers = {"User-Agent": VK_USER_AGENT}
    try:
        resp = await client.post(url, headers=headers, data=params, timeout=15.0)
    except httpx.HTTPError:
        # Fallback to direct connection without proxy if proxy timed out or failed
        direct_client = await get_http_client(enable_proxy=False)
        try:
            resp = await direct_client.post(url, headers=headers, data=params, timeout=15.0)
        except httpx.HTTPError as e:
            raise RuntimeError(f"VK API request failed: {e}") from e
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"VK API returned invalid JSON: {e}\nBody: {resp.text[:300]}")


async def _vk_get_anon_token() -> str:
    """Fetch anonymous VK API access token."""
    data = await _vk_api_request(
        "https://login.vk.com/?act=get_anonym_token",
        {
            "client_secret": VK_CLIENT_SECRET,
            "client_id": VK_CLIENT_ID,
            "scopes": "audio_anonymous,video_anonymous,photos_anonymous,profile_anonymous",
            "isApiOauthAnonymEnabled": "false",
            "version": "1",
            "app_id": VK_CLIENT_ID,
        },
    )
    if data.get("type") != "okay":
        raise RuntimeError(f"VK anon token error: {data}")
    return data["data"]["access_token"]


def _vk_parse_url(url: str) -> tuple[str, dict]:
    """Extract VK entity type (track, album, playlist) and identifiers from URL."""
    m = re.search(r"/audio(-?\d+)_(\d+)(?:_([0-9a-f]+))?", url)
    if m:
        return "track", {
            "owner_id": m.group(1),
            "audio_id": m.group(2),
            "access_key": m.group(3) or "",
        }
    m = re.search(r"/music/album/(-?\d+)_(\d+)(?:_([0-9a-f]+))?", url)
    if m:
        return "album", {
            "owner_id": m.group(1),
            "playlist_id": m.group(2),
            "access_key": m.group(3) or "",
        }
    m = re.search(r"/music/playlist/(-?\d+)_(\d+)(?:_([0-9a-f]+))?", url)
    if m:
        return "playlist", {
            "owner_id": m.group(1),
            "playlist_id": m.group(2),
            "access_key": m.group(3) or "",
        }
    raise RuntimeError(f"Cannot parse VK Music URL: {url}")


async def _vk_get_playlist_info(token: str, owner_id: str, playlist_id: str, access_key: str) -> dict:
    """Fetch playlist object from VK audio.getPlaylistById endpoint."""
    data = await _vk_api_request(
        f"{VK_API_URL}/audio.getPlaylistById",
        {
            "v": VK_API_VERSION,
            "client_id": VK_CLIENT_ID,
            "owner_id": owner_id,
            "playlist_id": playlist_id,
            "access_key": access_key,
            "access_token": token,
            "extra_fields": "owner,duration",
        },
    )
    if "error" in data:
        raise RuntimeError(f"VK audio.getPlaylistById error: {data['error']}")
    return data["response"]["playlist"]


async def _vk_get_audio_ids(token: str, owner_id: str, playlist_id: str, access_key: str) -> list[str]:
    """Fetch list of audio item IDs for a VK playlist."""
    entity_id = f"{owner_id}_{playlist_id}"
    if access_key:
        entity_id += f"_{access_key}"
    data = await _vk_api_request(
        f"{VK_API_URL}/audio.getIdsBySource",
        {
            "v": VK_API_VERSION,
            "client_id": VK_CLIENT_ID,
            "source": "playlist",
            "entity_id": entity_id,
            "access_token": token,
        },
    )
    if "error" in data:
        raise RuntimeError(f"VK audio.getIdsBySource error: {data['error']}")
    return [item["audio_id"] for item in data["response"]["audios"]]


async def _vk_get_audios_by_id(token: str, audio_ids: list[str]) -> list[dict]:
    """Fetch track metadata objects from VK audio.getById endpoint in batches."""
    results = []
    chunk_size = 50
    for i in range(0, len(audio_ids), chunk_size):
        chunk = audio_ids[i: i + chunk_size]
        data = await _vk_api_request(
            f"{VK_API_URL}/audio.getById",
            {
                "v": VK_API_VERSION,
                "client_id": VK_CLIENT_ID,
                "audios": ",".join(chunk),
                "client_secret": VK_CLIENT_SECRET,
                "access_token": token,
                "extra_fields": "owner,duration",
            },
        )
        if "error" in data:
            raise RuntimeError(f"VK audio.getById error: {data['error']}")
        results.extend(data.get("response", []))
    return results


async def resolve_owner_id(screen_name: str) -> str:
    """Resolve a VK screen name / id to a numeric owner id. Raises ProfileClosedError if not a user."""
    if screen_name.startswith("id") and screen_name[2:].isdigit():
        return screen_name[2:]
    token = await _vk_get_anon_token()
    data = await _vk_api_request(
        f"{VK_API_URL}/utils.resolveScreenName",
        {"v": VK_API_VERSION, "screen_name": screen_name, "access_token": token},
    )
    if "error" in data:
        raise ProfileClosedError(f"resolveScreenName error: {data['error'].get('error_msg', data['error'])}")
    resp = data.get("response", {})
    if not resp or resp.get("type") != "user":
        raise ProfileClosedError(f"not a user profile: {screen_name}")
    return str(resp["object_id"])


async def list_vk_user_tracks(owner_id: str, token: str | None = None) -> list[dict]:
    """List all public audio of a VK user via audio.get.

    Uses the provided token, falling back to the configured VK_ACCESS_TOKEN.
    Requires a personal (non-anonymous) token; the anonymous token cannot enumerate.
    """
    token = token or config.VK_ACCESS_TOKEN
    if not token:
        raise ProfileClosedError("VK_ACCESS_TOKEN not configured")
    total = 0
    all_tracks: list[dict] = []
    offset = 0
    while True:
        data = await _vk_api_request(
            f"{VK_API_URL}/audio.get",
            {
                "v": VK_API_VERSION,
                "owner_id": owner_id,
                "count": "200",
                "offset": str(offset),
                "access_token": token,
            },
        )
        if "error" in data:
            code = data["error"].get("error_code")
            # 5 = bad token, 15/30/201 = access denied / private profile
            raise ProfileClosedError(f"audio.get error {code}: {data['error'].get('error_msg', '')}")
        resp = data.get("response", {})
        items = resp.get("items", [])
        if not items:
            break
        all_tracks.extend(items)
        total = resp.get("count", 0)
        offset += len(items)
        if len(all_tracks) >= total:
            break
    log.info("vk user %s: listed %d tracks (vk total count=%d)", owner_id, len(all_tracks), total)
    return all_tracks


async def _download_vk_direct(
    url: str,
    artist: str,
    title: str,
    thumb_url: str | None,
    audio_format: str,
    tmpdir: Path,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> DownloadResult | None:
    """Download mp3 directly from VK CDN via httpx streaming, bypassing yt-dlp.

    VK serves audio as a single mp3 file (not HLS). When yt-dlp processes
    this URL it sometimes misidentifies the stream, causing segment-boundary
    glitches (clicks/silence at ~25-30s). Streaming the raw bytes with httpx
    avoids this entirely.

    Returns None if the URL turns out to be non-mp3 (e.g. m3u8), so the
    caller can fall back to yt-dlp or search.
    """
    if ".m3u8" in url:
        # genuine HLS — let yt-dlp handle it
        return None

    client = await get_http_client(enable_proxy=True)
    max_bytes = 50 * 1024 * 1024  # 50 MB safety cap
    afmt = AUDIO_FORMATS.get(audio_format, AUDIO_FORMATS["mp3_192"])
    ext = ".mp3"  # VK always serves mp3
    safe_stem = _safe_filename(f"{artist} - {title}")[:150].strip() or "track"
    out_path = tmpdir / f"{safe_stem}{ext}"

    if on_progress:
        await on_progress(f"Downloading '{artist} - {title}' from VK...")

    try:
        async with client.stream(
            "GET", url,
            headers={"User-Agent": VK_USER_AGENT},
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
        ) as resp:
            if resp.status_code != 200:
                log.warning("VK CDN returned %s for %s - %s", resp.status_code, artist, title)
                return None

            ct = resp.headers.get("content-type", "")
            if "mpegurl" in ct or "m3u8" in ct:
                return None  # HLS playlist, fall back

            total = int(resp.headers.get("content-length", 0) or 0)
            if total and total > max_bytes:
                raise FileTooLarge(f"file too large: ~{_human_size(total)}")

            downloaded = 0
            with open(out_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    if should_cancel and should_cancel():
                        raise DownloadCancelled("download cancelled")
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise FileTooLarge(f"file too large: >{_human_size(max_bytes)}")
    except (DownloadCancelled, FileTooLarge):
        raise
    except httpx.HTTPError as e:
        log.warning("httpx stream failed for %s - %s: %s", artist, title, e)
        return None

    if not out_path.exists() or out_path.stat().st_size < 1024:
        return None  # too small, probably an error page

    # Transcode to target format if needed (VK always gives mp3)
    need_transcode = afmt["codec"] not in ("mp3",)
    final_path = out_path
    if need_transcode:
        import asyncio
        target_ext = afmt["ext"]
        transcoded = tmpdir / f"{safe_stem}{target_ext}"
        ffmpeg_cmd = ["ffmpeg", "-y", "-i", str(out_path)]
        if afmt["codec"] == "flac":
            ffmpeg_cmd += ["-c:a", "flac"]
        elif afmt["codec"] == "m4a":
            ffmpeg_cmd += ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"]
        else:
            ffmpeg_cmd += ["-c:a", "libmp3lame", "-b:a", f"{afmt['quality']}k"]
        ffmpeg_cmd.append(str(transcoded))
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and transcoded.exists() and transcoded.stat().st_size > 0:
            final_path = transcoded
        else:
            log.warning("ffmpeg transcode failed: %s", stderr.decode(errors="ignore")[-300:])
            final_path = out_path  # keep original mp3

    # Embed cover art
    thumb_file = None
    if thumb_url:
        try:
            thumb_path = tmpdir / "thumbnail.jpg"
            resp_thumb = await client.get(thumb_url, timeout=15.0, follow_redirects=True)
            if resp_thumb.status_code == 200:
                thumb_path.write_bytes(resp_thumb.content)
                thumb_file = thumb_path
                if final_path.suffix.lower() == ".mp3":
                    _embed_cover_mp3(final_path, thumb_file)
        except Exception:  # noqa: BLE001
            log.debug("thumb download failed", exc_info=True)

    return DownloadResult(
        filepath=final_path,
        title=title,
        thumbnail=thumb_file,
        uploader=artist,
        is_audio=True,
        duration=0,
        filesize=final_path.stat().st_size,
    )


async def fetch_vk_track(
    track: dict,
    audio_format: str,
    tmpdir: Path,
    should_cancel: CancelCheck | None = None,
    lang: str = "ru",
    on_progress: ProgressCallback | None = None,
) -> DownloadResult:
    """Download a single VK track: direct mp3 URL when available, YouTube search fallback otherwise."""
    artist = track.get("artist") or "Unknown"
    title = track.get("title") or "Track"
    duration = track.get("duration")
    thumb_url = (track.get("thumb") or {}).get("photo_600") or (track.get("album", {}).get("thumb") or {}).get("photo_600")
    direct_url = track.get("url")
    if direct_url:
        try:
            # ponytail: download raw mp3 from VK CDN via httpx, bypass yt-dlp
            # to avoid HLS segment boundary glitches (clicks/pauses at ~26s)
            result = await _download_vk_direct(
                direct_url, artist, title, thumb_url,
                audio_format, tmpdir, on_progress, should_cancel,
            )
            if result:
                return result
        except (DownloadCancelled, FileTooLarge):
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("direct VK download failed for %s - %s, falling back to search: %s", artist, title, e)
    return await _download_track_search(
        artist, title, tmpdir,
        on_progress=on_progress,
        audio_format=audio_format,
        thumb_url=thumb_url,
        should_cancel=should_cancel,
        lang=lang,
        expected_duration=duration,
    )


async def download_vk_music(
        url: str,
        tmpdir: Path,
        on_progress: ProgressCallback | None = None,
        audio_format: str = "mp3_192",
        should_cancel: CancelCheck | None = None,
) -> DownloadResult:
    """Download VK Music track, album, or playlist.

    Args:
        url: VK Music track, album, or playlist URL.
        tmpdir: Directory path for temporary outputs.
        on_progress: Async progress updates callback function.
        audio_format: Codec/bitrate selection key.
        should_cancel: Cancellation condition check function.

    Returns:
        DownloadResult instance containing output media or ZIP path.
    """
    if on_progress:
        await on_progress("Getting VK token...")
    token = await _vk_get_anon_token()
    entity_type, params = _vk_parse_url(url)
    if entity_type == "track":
        audio_id = f"{params['owner_id']}_{params['audio_id']}"
        if params.get("access_key"):
            audio_id += f"_{params['access_key']}"
        if on_progress:
            await on_progress("Getting track info...")
        tracks = await _vk_get_audios_by_id(token, [audio_id])
        if not tracks:
            raise RuntimeError("VK API returned no track info")
        track = tracks[0]
        return await fetch_vk_track(
            track, audio_format, tmpdir,
            should_cancel=should_cancel,
            on_progress=on_progress,
        )
    owner_id = params["owner_id"]
    playlist_id = params["playlist_id"]
    access_key = params.get("access_key", "")
    if on_progress:
        await on_progress("Getting playlist info...")
    playlist_info = await _vk_get_playlist_info(token, owner_id, playlist_id, access_key)
    album_title = playlist_info.get("title", "VK Playlist")
    main_artists = playlist_info.get("main_artists", [])
    album_artist = main_artists[0]["name"] if main_artists else ""
    cover_url: str = (playlist_info.get("photo") or {}).get("photo_600", "")
    album_artist_final = album_artist or "VK Music"
    if on_progress:
        await on_progress(f"Downloading tracks '{album_title}'...")
    audio_ids = await _vk_get_audio_ids(token, owner_id, playlist_id, access_key)
    if not audio_ids:
        raise RuntimeError("VK playlist contains no tracks")
    tracks = await _vk_get_audios_by_id(token, audio_ids)
    async def _vk_download_one(track: dict, idx: int, total: int, track_tmpdir: Path) -> DownloadResult:
        artist = track.get("artist", album_artist or "Unknown")
        title = track.get("title", f"Track {idx}")
        track_thumb = (track.get("thumb") or {}).get("photo_600") or cover_url or None
        if on_progress:
            await on_progress(f"Downloading {idx}/{total}: {artist} - {title}")
        return await _download_track_search(
            artist, title, track_tmpdir,
            audio_format=audio_format,
            thumb_url=track_thumb,
            should_cancel=should_cancel,
            expected_duration=track.get("duration"),
        )
    return await _process_collection_tracks(
        tracks, album_title, cover_url, tmpdir,
        on_progress=on_progress,
        should_cancel=should_cancel,
        download_one=_vk_download_one,
        uploader=album_artist_final,
    )

