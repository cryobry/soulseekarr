#!/usr/bin/env python
from __future__ import annotations
import argparse
from typing import ClassVar, Literal, Self
import random
import re
import os
import sys
import time
import shutil
import logging
import fcntl
import unicodedata
from urllib.parse import urljoin
from dataclasses import dataclass, field
import music_tag
from rapidfuzz import fuzz
import slskd_api
import yaml
from collections import Counter, deque
from requests.exceptions import HTTPError
from pyarr import LidarrAPI
from pyarr.exceptions import PyarrError
from migration import expand_env_vars, migrate_soularr_ini_config

logger = logging.getLogger("soulseekarr")

DEFAULT_LOGGING = {
    "level": "INFO",
    "format": "[%(levelname)s|%(name)s|L%(lineno)d] %(asctime)s: %(message)s",
    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
    "log_to_file": True,
    "log_file": "soulseekarr.log",
    "max_bytes": 1048576,
    "backup_count": 3,
}

AlbumSource = Literal["missing", "cutoff_unmet"]


@dataclass(frozen=True, slots=True)
class DownloadSource:
    """A complete directory source for one candidate album attempt."""

    username: str
    file_dir: str
    files: tuple[dict, ...]
    required_names: frozenset[str]
    has_free_upload_slot: bool
    upload_speed: float
    queue_length: int
    disk_no: int | None = None
    disk_count: int | None = None


@dataclass(slots=True)
class DownloadCandidate:
    """A validated album release that can be enqueued after search cleanup."""

    sources: list[DownloadSource]
    match_score: float
    filetype_rank: int
    release_rank: int

    @property
    def has_free_upload_slot(self) -> bool:
        return all(source.has_free_upload_slot for source in self.sources)

    @property
    def upload_speed(self) -> float:
        return min(source.upload_speed for source in self.sources)

    @property
    def queue_length(self) -> int:
        return max(source.queue_length for source in self.sources)

    @property
    def identity(self) -> tuple:
        return tuple(sorted((
            source.username,
            source.file_dir,
            source.disk_no,
            tuple(sorted(source.required_names)),
        ) for source in self.sources))

    @property
    def sort_key(self) -> tuple:
        return (
            self.filetype_rank,
            -self.match_score,
            not self.has_free_upload_slot,
            -self.upload_speed,
            self.queue_length,
            self.release_rank,
        )

    @property
    def location(self) -> str:
        return ", ".join(f"{source.username}\\{source.file_dir}" for source in self.sources)

    @property
    def summary(self) -> str:
        return (
            f"{self.location}, score={self.match_score:.3f}, "
            f"free-slot={self.has_free_upload_slot}, "
            f"upload-speed={self.upload_speed:g}, "
            f"queue-length={self.queue_length}"
        )


@dataclass
class AlbumState:
    """State shared by every instance representing the same Lidarr album."""

    queued: bool = False
    grabbed: bool = False
    import_failed: bool = False
    import_pending: bool = False


@dataclass
class LidarrConfig:
    api_key: str
    host_url: str
    url_base: str
    download_dir: str
    import_timeout: int
    disable_sync: bool
    sources: list[AlbumSource]
    search_type: str
    shuffle_all: bool
    chunk_size: int
    sort_key: str
    sort_dir: str
    title_blacklist: list[str]
    failed_import_denylist: bool
    use_selected_lidarr_release: bool
    use_most_common_tracknum: bool
    allow_multi_disc: bool
    accepted_countries: list[str]
    skip_region_check: bool
    accepted_formats: list[str]


@dataclass
class SlskdConfig:
    api_key: str
    host_url: str
    download_dir: str
    failed_imports_dir: str
    url_base: str
    monitor_downloads: bool
    stalled_timeout: int
    remote_queue_timeout: int
    remove_searches: bool
    remove_completed_downloads: bool
    requeue_failed_downloads: bool
    search_timeout: int
    maximum_peer_queue: int
    minimum_peer_upload_speed: int
    minimum_match_ratio: float
    ignored_users: list[str]
    search_blacklist: list[str]
    album_prepend_artist: bool
    filtering: bool
    extensions_whitelist: list[str]
    rename_download_folders: bool
    allowed_filetypes: list[str]


@dataclass
class AppConfig:
    source: AlbumSource | None
    lidarr: LidarrConfig
    slskd: SlskdConfig
    interval: int

    @classmethod
    def from_yaml(cls, data: dict, args, source: AlbumSource | None = None) -> "AppConfig":
        """Build an AppConfig from parsed config.yml data, env var overrides, and CLI args.

        Layers this source's overrides (top-level `missing`/`cutoff_unmet` blocks) over the
        top-level `lidarr`/`slskd` defaults, then applies per-key env var overrides.
        """
        lidarr_cfg: dict = data.get("lidarr") or {}
        slskd_cfg: dict = data.get("slskd") or {}

        # Layer this source's overrides (top-level `missing`/`cutoff_unmet` blocks) over the top-level lidarr/slskd defaults.
        source_cfg: dict = (data.get(source) or {}) if source else {}
        resolved_lidarr = {**lidarr_cfg, **(source_cfg.get("lidarr") or {})}
        resolved_slskd = {**slskd_cfg, **(source_cfg.get("slskd") or {})}

        def setting(section: str, key: str, default=None):
            values = resolved_lidarr if section == "lidarr" else resolved_slskd
            return env_override(section, key, values.get(key, default))

        sources = list(setting("lidarr", "sources", ["missing"]))
        invalid_sources = [source for source in sources if source not in ("missing", "cutoff_unmet")]
        if invalid_sources:
            raise ValueError(f"LIDARR_SOURCES contains unsupported values: {invalid_sources}")
        search_type = str(setting("lidarr", "search_type", "incrementing")).lower().strip()
        if search_type not in ("incrementing", "shuffle"):
            raise ValueError(f"[lidarr.search_type] - {search_type = } is not valid")
        lidarr = LidarrConfig(
            api_key=setting("lidarr", "api_key"),
            host_url=setting("lidarr", "host_url"),
            url_base=str(setting("lidarr", "url_base", "/")),
            download_dir=setting("lidarr", "download_dir"),
            import_timeout=int(setting("lidarr", "import_timeout", 3600)),
            disable_sync=bool(setting("lidarr", "disable_sync", False)),
            sources=sources,
            search_type=search_type,
            shuffle_all=bool(env_override("lidarr", "shuffle_all", lidarr_cfg.get("shuffle_all", False))),
            chunk_size=int(setting("lidarr", "chunk_size", 10)),
            sort_key=str(setting("lidarr", "sort_key", "albums.title")).strip(),
            sort_dir=str(setting("lidarr", "sort_dir", "ascending")).strip().lower(),
            title_blacklist=setting("lidarr", "title_blacklist", []),
            failed_import_denylist=bool(setting("lidarr", "failed_import_denylist", True)),
            use_selected_lidarr_release=bool(setting("lidarr", "use_selected_lidarr_release", False)),
            use_most_common_tracknum=bool(setting("lidarr", "use_most_common_tracknum", True)),
            allow_multi_disc=bool(setting("lidarr", "allow_multi_disc", True)),
            accepted_countries=setting(
                "lidarr", "accepted_countries",
                ["Europe", "Japan", "United Kingdom", "United States", "[Worldwide]", "Australia", "Canada"],
            ),
            skip_region_check=bool(setting("lidarr", "skip_region_check", False)),
            accepted_formats=setting("lidarr", "accepted_formats", ["CD", "Digital Media", "Vinyl"]),
        )

        # Pre-set this so we can derive the fallback failed_imports_dir in the download dir
        slskd_download_dir = setting("slskd", "download_dir")

        slskd = SlskdConfig(
            api_key=setting("slskd", "api_key"),
            host_url=setting("slskd", "host_url"),
            download_dir=slskd_download_dir,
            failed_imports_dir=str(
                setting("slskd", "failed_imports_dir") or os.path.join(slskd_download_dir, ".failed_imports")
            ),
            url_base=str(setting("slskd", "url_base", "/")),
            monitor_downloads=bool(setting("slskd", "monitor_downloads", True)),
            stalled_timeout=int(setting("slskd", "stalled_timeout", 3600)),
            remote_queue_timeout=int(setting("slskd", "remote_queue_timeout", 300)),
            remove_searches=bool(setting("slskd", "remove_searches", True)),
            remove_completed_downloads=bool(setting("slskd", "remove_completed_downloads", True)),
            requeue_failed_downloads=bool(setting("slskd", "requeue_failed_downloads", True)),
            search_timeout=int(setting("slskd", "search_timeout", 10)),
            maximum_peer_queue=int(setting("slskd", "maximum_peer_queue", 50)),
            minimum_peer_upload_speed=int(setting("slskd", "minimum_peer_upload_speed", 0)),
            minimum_match_ratio=float(setting("slskd", "minimum_filename_match_ratio", 0.5)),
            ignored_users=setting("slskd", "ignored_users", []),
            search_blacklist=setting("slskd", "search_blacklist", []),
            album_prepend_artist=bool(setting("slskd", "album_prepend_artist", False)),
            filtering=bool(setting("slskd", "filtering", False)),
            extensions_whitelist=setting("slskd", "extensions_whitelist", ["txt", "nfo", "jpg"]),
            rename_download_folders=bool(setting("slskd", "rename_download_folders", True)),
            allowed_filetypes=setting("slskd", "allowed_filetypes", ["flac", "mp3"]),
        )

        return cls(
            source=source,
            lidarr=lidarr,
            slskd=slskd,
            interval=int(
                args.interval if args.interval is not None else os.getenv(
                    "LOOP_INTERVAL",
                    os.getenv("SCRIPT_INTERVAL", data.get("interval", 300 if is_docker() else 0)),
                )
            ),
        )


@dataclass
class WantedAlbum:
    """A pruned Lidarr wanted album and its current search state.

    Lidarr's raw wanted-list response nests far more per album (images, ratings, full release
    media/tracks, statistics, etc.); trimming to this shape keeps memory use sane for large
    libraries.
    """

    id: int
    artistId: int
    artist: str
    title: str
    year: str
    cfg: AppConfig = field(repr=False)
    state: AlbumState = field(init=False, repr=False)

    _states: ClassVar[dict[int, AlbumState]] = {}
    failure: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.state = self._states.setdefault(self.id, AlbumState())

    @classmethod
    def prune_wanted_record(cls, raw: dict, cfg: AppConfig) -> "WantedAlbum":
        """Build a wanted album from the fields used by Soulseekarr."""
        return cls(
            id=raw["id"],
            artistId=raw["artistId"],
            artist=raw["artist"]["artistName"],
            title=raw["title"],
            year=raw["releaseDate"][0:4],
            cfg=cfg,
        )

    def skip_reason(self) -> str | None:
        """Return why this album should be skipped, or None if it is eligible."""
        if self.state.queued:
            return "already queued"
        if self.cfg.lidarr.failed_import_denylist and self.state.import_failed:
            return "failed import"
        if self.state.import_pending:
            return "pending Lidarr import"
        if self.cfg.lidarr.disable_sync and self.state.grabbed:
            return "already grabbed"

        for word in self.cfg.lidarr.title_blacklist:
            if word and word.lower() in self.title.lower():
                return f"blacklisted word {word!r}"

        return None


@dataclass
class AlbumSearch:
    """Search state and matching caches for one wanted album."""

    MAX_CANDIDATES = 10
    MULTI_SEARCH_LIMIT = 100
    _TRACK_PREFIX: ClassVar[re.Pattern[str]] = re.compile(
        r"^\s*(?:(?:cd|disc)\s*\d{1,2}\s*[-.]?\s*|\d{1,2}\s*[-.]\s*|track\s+|a)?"
        r"(?P<track>\d{1,2})(?:\s*[-.)_]\s*|\s+|_+)",
        re.IGNORECASE,
    )
    _TRAILING_METADATA: ClassVar[re.Pattern[str]] = re.compile(
        r"\s+(?:(?:19|20)\d{2}\s+)?"
        r"(?:anniversary|deluxe|expanded|reissue(?:d)?|remaster(?:ed)?)"
        r"(?:\s+edition)?(?:\s+(?:19|20)\d{2})?$"
    )

    album: WantedAlbum
    id: str | None = None
    deadline: float = 0.0
    results: dict = field(default_factory=dict)
    folder_cache: dict = field(default_factory=dict)
    match_cache: dict = field(default_factory=dict)

    @property
    def artist(self) -> str:
        return self.album.artist

    @property
    def title(self) -> str:
        return self.album.title

    @property
    def cfg(self) -> AppConfig:
        return self.album.cfg

    def start_search(self) -> bool:
        """Start this album's slskd search without waiting for results."""
        self.album.failure = None
        self.id = None

        include_artist = len(self.title) == 1 or self.cfg.slskd.album_prepend_artist
        query = f"{self.artist} {self.title}" if include_artist else self.title
        original_query = query

        for word in filter(None, self.cfg.slskd.search_blacklist):
            query = re.sub(re.escape(word), "", query, flags=re.IGNORECASE)

        query = " ".join(query.split())

        if query != original_query:
            logger.info(f"Filtered search query: '{original_query}' -> '{query}'")
        if not query:
            logger.warning(f"Skipping {self.artist} - {self.title}: empty search query")
            self.album.failure = "Search failed"
            return False

        logger.info(f"Searching for '{self.cfg.source}' album: {self.artist} - {self.title}")
        logger.debug(f"Search query: '{query}'")

        search_timeout = max(1, self.cfg.slskd.search_timeout)
        self.deadline = time.monotonic() + search_timeout + 5

        try:
            search = slskd.searches.search_text(
                searchText=query,
                searchTimeout=search_timeout * 1000,
                filterResponses=True,
                maximumPeerQueueLength=self.cfg.slskd.maximum_peer_queue,
                minimumPeerUploadSpeed=self.cfg.slskd.minimum_peer_upload_speed,
            )
            self.id = search["id"]
            return True

        except Exception:
            self.album.failure = "Search failed"
            logger.exception(f"Failed to start search via SLSKD: {query}")
            return False

    def search_finished(self, state: dict | None = None) -> Literal["complete", "pending", "missing"]:
        """Return the search status, distinguishing a confirmed disappeared search."""
        if self.id is None:
            return "complete"

        if state is None:
            try:
                state = slskd.searches.state(self.id, False)
            except HTTPError as ex:
                if ex.response is not None and ex.response.status_code == 404:
                    return "missing"
                logger.exception(f"Failed to check SLSKD search for {self.artist} - {self.title}")
                return "pending"
            except Exception:
                logger.exception(f"Failed to check SLSKD search for {self.artist} - {self.title}")
                return "pending"

        if state.get("isComplete"):
            return "complete"

        now = time.monotonic()
        if now >= self.deadline:
            logger.warning(
                f"SLSKD search for {self.artist} - {self.title} did not complete "
                f"within its {self.cfg.slskd.search_timeout}s timeout + 5s grace; stopping it"
            )
            try:
                if not slskd.searches.stop(self.id):
                    logger.warning(f"SLSKD did not stop search {self.id}")
            except Exception:
                logger.warning(f"Failed to stop SLSKD search {self.id}", exc_info=True)
            self.deadline = now + 5

        return "pending"

    def collect_search(self) -> bool:
        """Collect this search's results, cache them, and remove the search."""
        search_id = self.id
        try:
            responses = slskd.searches.search_responses(search_id)
            logger.info(f"Search for {self.artist} - {self.title} returned {len(responses)} results")
            if not responses:
                self.album.failure = "Search failed"
                return False

            self.results.clear()
            for result in responses:
                username = result["username"]
                if username in self.cfg.slskd.ignored_users:
                    logger.debug(f"Skipping ignored user: {username}")
                    continue
                user_results = self.results.setdefault(username, {})
                logger.debug(f"Caching and truncating results for user: {username}")
                peer = {
                    "hasFreeUploadSlot": bool(result.get("hasFreeUploadSlot", False)),
                    "uploadSpeed": result.get("uploadSpeed") or 0,
                    "queueLength": result.get("queueLength") or 0,
                }
                for file in result["files"]:
                    file_dir = file["filename"].rsplit("\\", 1)[0]
                    for filetype in self.cfg.slskd.allowed_filetypes:
                        if verify_filetype(file, filetype):
                            directories = user_results.setdefault(filetype, [])
                            if not any(item["file_dir"] == file_dir for item in directories):
                                directories.append({"file_dir": file_dir, **peer})
            return True
        except Exception:
            self.album.failure = "Search failed"
            logger.exception(f"Failed to collect SLSKD search for {self.artist} - {self.title}")
            return False
        finally:
            self.id = None
            self.deadline = 0.0

            if search_id and self.cfg.slskd.remove_searches:
                try:
                    if not slskd.searches.delete(search_id):
                        logger.warning(f"SLSKD did not delete search {search_id}")
                except Exception:
                    logger.warning(f"Failed to delete search {search_id} from SLSKD", exc_info=True)

    def queue_download(self) -> AlbumDownload | None:
        """Enqueue the best match from this album's cached search results."""
        if not self.results:
            self.album.failure = "Search failed"
            return None

        download = AlbumDownload(album=self.album, candidates=deque(self.discover_candidates()))
        if download.enqueue_next():
            return download
        if self.album.failure is None:
            self.album.failure = "No matching download"
        return None

    def discover_candidates(self) -> list[DownloadCandidate]:
        """Discover and rank complete candidates without starting downloads."""
        results = self.results
        releases = self.ordered_releases(lidarr.get_album(self.album.id)["releases"])
        tracks_by_release = {}
        candidates = {}

        for filetype_rank, filetype in enumerate(self.cfg.slskd.allowed_filetypes):
            if not any(filetype in result for result in results.values()):
                logger.debug(f"No search results for {filetype}. Skipping.")
                continue

            for release_rank, release in enumerate(releases):
                release_id = release["id"]
                if release_id not in tracks_by_release:
                    tracks_by_release[release_id] = lidarr.get_tracks(
                        artistId=self.album.artistId,
                        albumId=self.album.id,
                        albumReleaseId=release_id,
                    )
                tracks = tracks_by_release[release_id]

                if len(release["media"]) > 1:
                    found = self.discover_multi_candidates(release, tracks, results, filetype, filetype_rank, release_rank)
                else:
                    found = [
                        DownloadCandidate([source], score, filetype_rank, release_rank)
                        for source, score in self._discover_sources(tracks, results, filetype)
                    ]

                for candidate in found:
                    identity = candidate.identity
                    current = candidates.get(identity)
                    if current is None or candidate.sort_key < current.sort_key:
                        candidates[identity] = candidate

            if len(candidates) >= self.MAX_CANDIDATES:
                break

        return sorted(candidates.values(), key=lambda candidate: candidate.sort_key)[:self.MAX_CANDIDATES]

    def discover_multi_candidates(self, release, tracks, results, filetype, filetype_rank, release_rank) -> list[DownloadCandidate]:
        """Discover complete combinations of distinct sources for every disc."""
        media = release["media"]
        if not self.cfg.lidarr.allow_multi_disc or len(media) <= 1:
            return []

        matches_by_disc = []
        for medium in media:
            disk_no = medium["mediumNumber"]
            disk_tracks = [track for track in tracks if track["mediumNumber"] == disk_no]
            disk_matches = self._discover_sources(disk_tracks, results, filetype, disk_no, len(media))
            if not disk_matches:
                return []
            disk_matches.sort(key=lambda item: (
                -item[1],
                not item[0].has_free_upload_slot,
                -item[0].upload_speed,
                item[0].queue_length,
            ))
            matches_by_disc.append(disk_matches)

        matches_by_disc.sort(key=len)

        candidates = []
        nodes = 0
        node_limit = self.MULTI_SEARCH_LIMIT * len(matches_by_disc)

        def collect(index, sources, score_sum, track_count, used_sources):
            nonlocal nodes
            if nodes >= node_limit or len(candidates) >= self.MULTI_SEARCH_LIMIT:
                return
            nodes += 1
            if index == len(matches_by_disc):
                candidates.append(DownloadCandidate(sources, score_sum / track_count, filetype_rank, release_rank))
                return

            for source, match_score in matches_by_disc[index]:
                source_key = (source.username, source.file_dir)
                if source_key in used_sources:
                    continue
                source_track_count = len(source.required_names)
                collect(
                    index + 1,
                    sources + [source],
                    score_sum + match_score * source_track_count,
                    track_count + source_track_count,
                    used_sources | {source_key},
                )

        collect(0, [], 0.0, 0, set())
        candidates.sort(key=lambda item: item.sort_key)
        return candidates[:self.MAX_CANDIDATES]

    def _discover_sources(self, tracks, results, filetype, disk_no=None, disk_count=None):
        sources = []
        for username, user_results in results.items():
            for peer in user_results.get(filetype, []):
                logger.debug(f"Parsing result from user: {username}")
                match = self.match_directory(tracks, filetype, peer["file_dir"], username)
                if not match:
                    continue
                source_files, required_files, score = match
                sources.append((DownloadSource(
                    username=username,
                    file_dir=peer["file_dir"],
                    files=source_files,
                    required_names=frozenset(file["filename"] for file in required_files),
                    has_free_upload_slot=bool(peer.get("hasFreeUploadSlot", False)),
                    upload_speed=float(peer.get("uploadSpeed") or 0),
                    queue_length=int(peer.get("queueLength") or 0),
                    disk_no=disk_no,
                    disk_count=disk_count,
                ), score))
        return sources

    def match_directory(self, tracks, filetype, file_dir, username):
        """Return a complete, reasonably sized match for one directory."""
        match_key = (
            username,
            file_dir,
            filetype,
            tuple((track["title"], track.get("trackNumber"), track.get("mediumNumber")) for track in tracks),
        )
        if match_key in self.match_cache:
            return self.match_cache[match_key]

        user_cache = self.folder_cache.setdefault(username, {})
        track_count = len(tracks)
        maximum_audio_files = max(track_count * 2, track_count + 10)
        maximum_files = maximum_audio_files + 25

        directory = user_cache.get(file_dir)
        if directory is None:
            try:
                directory = slskd.users.directory(
                    username=username,
                    directory=file_dir,
                )[0]
            except IndexError:
                logger.warning(f'Empty directory response for "{username}\\{file_dir}"')
                directory = {"files": []}
            except HTTPError as ex:
                status = ex.response.status_code if ex.response is not None else "unknown"
                logger.warning(
                    f'Failed to read directory "{username}\\{file_dir}" from SLSKD '
                    f"(HTTP {status}); skipping source"
                )
                return None
            except Exception:
                logger.warning(
                    f'Failed to read directory "{username}\\{file_dir}"',
                    exc_info=True,
                )
                return None

            user_cache[file_dir] = directory

        directory_files = directory["files"]
        audio_files = []
        source_files = []
        if self.cfg.slskd.filtering:
            extensions = {ext.lower() for ext in self.cfg.slskd.extensions_whitelist}
            extensions.add(filetype.split()[0].lower())
        else:
            extensions = None

        for file in directory_files:
            if verify_filetype(file, filetype):
                audio_files.append(file)
            if extensions is None or file["filename"].rsplit(".", 1)[-1].lower() in extensions:
                source_files.append(file)

        if len(audio_files) < track_count:
            self.match_cache[match_key] = None
            return None
        if len(audio_files) > maximum_audio_files:
            logger.debug(
                f'Skipping oversized directory "{username}\\{file_dir}": {len(audio_files)} '
                f"{filetype} files for {track_count} expected tracks"
            )
            self.match_cache[match_key] = None
            return None

        source_files = tuple(source_files)

        if len(source_files) > maximum_files:
            logger.warning(
                f"Rejecting oversized match for {self.artist} - {self.title} "
                f'from "{username}\\{file_dir}": '
                f"{len(source_files)} filtered files for {track_count} expected tracks"
            )
            self.match_cache[match_key] = None
            return None

        matched = self.album_match(tracks, audio_files)
        result = (source_files, *matched) if matched else None
        self.match_cache[match_key] = result
        return result

    def album_match(self, lidarr_tracks, slskd_tracks):
        """Return a one-to-one file match for every Lidarr track, or None.

        Compares each Lidarr track title against every candidate filename with fuzzy string
        matching (plus several filename-cleanup heuristics), requiring every track to clear
        `slskd.minimum_match_ratio` for the album to count as matched. Each candidate file can
        only be assigned to one track.
        """

        if not lidarr_tracks:
            return None

        minimum_score = self.cfg.slskd.minimum_match_ratio
        available_files = sorted(
            (
                (
                    slskd_track,
                    self._prepare_filename(slskd_track["filename"]),
                )
                for slskd_track in slskd_tracks
            ),
            key=lambda entry: (entry[1][0], str(entry[0]["filename"]).casefold()),
        )
        normalized_prefix = self._normalize_text(f"{self.artist} {self.title}")
        prepared_tracks = [
            (
                self._normalize_text(track["title"]),
                self._parse_track_number(track.get("trackNumber")),
            )
            for track in lidarr_tracks
        ]
        score_matrix = [
            [
                self._track_match_score(
                    normalized_prefix,
                    normalized_title,
                    candidate_parts,
                    track_number,
                )
                for _, candidate_parts in available_files
            ]
            for normalized_title, track_number in prepared_tracks
        ]
        assignment = self._assign_tracks(
            score_matrix,
            minimum_score,
        )
        if assignment is None:
            logger.debug(f"No complete track assignment for {self.artist} - {self.title}")
            return None

        matched_files = [available_files[column][0] for column in assignment]
        assigned_scores = [score_matrix[row][column] for row, column in enumerate(assignment)]
        mean_score = sum(assigned_scores) / len(assigned_scores)
        weakest_score = min(assigned_scores)
        album_score = 0.75 * mean_score + 0.25 * weakest_score
        logger.debug(
            f"Matched {self.artist} - {self.title}: minimum={weakest_score:.3f}, "
            f"mean={mean_score:.3f}, album={album_score:.3f}"
        )
        return matched_files, album_score

    @staticmethod
    def _assign_tracks(scores: list[list[float]], minimum: float) -> list[int] | None:
        """Find a complete one-to-one assignment, preferring stronger matches."""
        choices = [
            [
                file
                for file, score in sorted(enumerate(row), key=lambda item: item[1], reverse=True)
                if score >= minimum
            ]
            for row in scores
        ]
        if any(not options for options in choices):
            return None

        file_to_track = {}

        def assign(track, seen):
            for file in choices[track]:
                if file in seen:
                    continue
                seen.add(file)
                if file not in file_to_track or assign(file_to_track[file], seen):
                    file_to_track[file] = track
                    return True
            return False

        for track in sorted(range(len(scores)), key=lambda track: len(choices[track])):
            if not assign(track, set()):
                return None

        assignment = [0] * len(scores)
        for file, track in file_to_track.items():
            assignment[track] = file
        return assignment

    @classmethod
    def _prepare_filename(cls, candidate):
        basename = str(candidate).replace("\\", "/").rsplit("/", 1)[-1]
        stem = os.path.splitext(basename)[0]
        normalized = cls._normalize_text(stem)
        prefix_match = cls._TRACK_PREFIX.match(stem)
        without_prefix = stem[prefix_match.end():] if prefix_match else stem
        without_prefix = cls._normalize_text(without_prefix)
        return (
            normalized,
            cls._TRAILING_METADATA.sub("", without_prefix),
            int(prefix_match.group("track")) if prefix_match else None,
        )

    @staticmethod
    def _normalize_text(value):
        decomposed = unicodedata.normalize("NFKD", str(value))
        without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
        without_apostrophes = re.sub(r"['\u0060\u00b4\u2018\u2019\u02bc]", "", without_marks)
        without_apostrophes = without_apostrophes.replace("&", " and ")
        return re.sub(r"[\W_]+", " ", without_apostrophes.casefold(), flags=re.UNICODE).strip()

    @staticmethod
    def _parse_track_number(value):
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
            return int(value)
        return None

    @staticmethod
    def _track_match_score(prefix, title, candidate, expected_number=None) -> float:
        full, cleaned, candidate_number = candidate
        expected = (title, f"{prefix} {title}") if prefix else (title,)
        score = max(
            fuzz.ratio(wanted, actual)
            for wanted in expected
            for actual in (full, cleaned)
        ) / 100

        words = title.split()
        if len(words) > 1:
            suffix = " ".join(cleaned.split()[-len(words):])
            score = max(score, fuzz.ratio(title, suffix) / 100)

        if expected_number is not None and candidate_number is not None:
            if expected_number == candidate_number:
                score += 0.03
            else:
                score -= 0.04
        return max(0.0, min(1.0, score))

    def ordered_releases(self, releases):
        """Return eligible releases in the configured preference order."""
        cfg = self.cfg.lidarr

        def is_multi_disc(release):
            return len(release.get("media", [])) > 1 or release.get("mediumCount", 1) > 1

        if cfg.use_selected_lidarr_release:
            selected = next(
                (release for release in releases if release.get("monitored") and (cfg.allow_multi_disc or not is_multi_disc(release))),
                None,
            )
            if selected is not None:
                logger.info(
                    f"Using selected Lidarr release for {self.artist}: {selected['format']}, {selected['trackCount']} tracks, ID: {selected['id']}"
                )
                return [selected]

        def eligible(release):
            country = release["country"][0] if release["country"] else None
            return (
                (cfg.allow_multi_disc or not is_multi_disc(release)) and (cfg.skip_region_check or country in cfg.accepted_countries) and
                self.release_format_accepted(release) and release["status"] == "Official"
            )

        eligible_releases = [release for release in releases if eligible(release)]
        if cfg.use_most_common_tracknum and eligible_releases:
            track_count = Counter(release["trackCount"] for release in eligible_releases).most_common(1)[0][0]
            eligible_releases = [release for release in eligible_releases if release["trackCount"] == track_count]
        return eligible_releases

    def release_format_accepted(self, release) -> bool:
        """Check a release's format against `accepted_formats`, unwrapping multi-disc prefixes (e.g. "2xCD" -> "CD")."""
        fmt = release["format"]
        match = re.match(r"^\d+x", fmt, re.IGNORECASE)
        if match:
            if not self.cfg.lidarr.allow_multi_disc:
                return False
            fmt = fmt[match.end():]
        return fmt in self.cfg.lidarr.accepted_formats


class WantedQueue(list[WantedAlbum]):
    """A backlog or processing batch of wanted albums."""

    SEARCH_SLOTS = 2

    def take(self, count: int) -> Self:
        """Remove and return the first `count` albums."""
        selected = self[:count]
        del self[:count]
        return type(self)(selected)

    def shuffle(self) -> None:
        random.shuffle(self)

    def deduplicate(self) -> Self:
        """Keep one album per ID, preferring the stricter cutoff-unmet source."""
        unique_albums: dict[int, WantedAlbum] = {}
        for album in self:
            current = unique_albums.get(album.id)
            if current is None or album.cfg.source == "cutoff_unmet":
                unique_albums[album.id] = album
        return type(self)(unique_albums.values())

    def filter_eligible(self) -> Self:
        """Return albums that are eligible to be searched."""
        filtered = []

        for album in self:
            reason = album.skip_reason()
            if reason:
                logger.info(f"Skipping {reason}: {album.artist} - {album.title} (ID: {album.id})")
            else:
                filtered.append(album)

        return type(self)(filtered)

    def next_batch(self, cfg: AppConfig) -> Self:
        """Return the next batch of eligible wanted albums for this source."""
        if not self:
            wanted_kwargs = {
                "missing": cfg.source == "missing",
                "page_size": 250,
                "sort_key": "id" if cfg.lidarr.search_type == "shuffle" else cfg.lidarr.sort_key,
                "sort_dir": "ascending" if cfg.lidarr.search_type == "shuffle" else cfg.lidarr.sort_dir,
            }
            try:
                wanted_page = lidarr.get_wanted(page=1, **wanted_kwargs)
            except PyarrError as ex:
                logger.error(f"Failed to get wanted records: {ex}")
                return type(self)()

            raw_albums = list(wanted_page["records"])
            total_albums = wanted_page["totalRecords"]
            page = 2
            while len(raw_albums) < total_albums:
                try:
                    wanted_page = lidarr.get_wanted(page=page, **wanted_kwargs)
                except PyarrError as ex:
                    logger.error(f"Failed to get wanted page {page}: {ex}")
                    break

                page_records = wanted_page["records"]
                if not page_records:
                    logger.warning("Lidarr returned an empty page before totalRecords was reached")
                    break

                raw_albums.extend(page_records)
                page += 1

            self[:] = [WantedAlbum.prune_wanted_record(raw, cfg) for raw in raw_albums]

            if cfg.lidarr.search_type == "shuffle" and not cfg.lidarr.shuffle_all:
                logger.info(f"search_type: shuffle (shuffling '{cfg.source}' albums)")
                self.shuffle()

        eligible = self.filter_queued().filter_eligible()
        self[:] = eligible
        return self.take(max(1, cfg.lidarr.chunk_size))

    def grab_most_wanted(self, monitor_downloads: bool = True) -> int:
        """Search and enqueue every album, optionally waiting for completion."""
        downloads = self.search_and_queue()

        logger.info(f"Total downloads added: {len(downloads)}")
        for album in downloads:
            logger.info(f"Album: {album.title} Artist: {album.artist}")
        if monitor_downloads:
            downloads.monitor_downloads()
        else:
            for download in downloads:
                download.album.state.queued = True
            logger.info("Download monitoring disabled; continuing to the next Lidarr batch")

        failures = [album for album in self if album.failure]
        logger.info(f"Failed albums: {len(failures)}")

        for album in failures:
            logger.info(f"{album.failure}: {album.artist} - {album.title}")

        return len(failures)

    def search_and_queue(self) -> DownloadBatch:
        """Keep two slskd searches pending and enqueue completed results."""
        remaining = deque(self)
        pending: list[AlbumSearch] = []
        downloads = DownloadBatch()
        disappeared_ids: set[str] = set()

        def fill_search_slots() -> None:
            while remaining and len(pending) < self.SEARCH_SLOTS:
                album = remaining.popleft()
                search = AlbumSearch(album)
                if search.start_search():
                    pending.append(search)

        fill_search_slots()

        while pending:
            try:
                states = {search["id"]: search for search in slskd.searches.get_all()}
            except Exception:
                logger.exception("Failed to get SLSKD search states")
                time.sleep(1)
                continue

            completed = []
            for search in pending.copy():
                result = search.search_finished(states.get(search.id))
                if result == "pending":
                    continue

                pending.remove(search)
                if result == "missing":
                    search_id = search.id
                    if search_id not in disappeared_ids:
                        disappeared_ids.add(search_id)
                        search.id = None
                        search.deadline = 0.0
                        remaining.append(search.album)
                        logger.warning(
                            f"SLSKD search disappeared for {search.artist} - {search.title}; starting a new search"
                        )
                    continue

                if search.collect_search():
                    completed.append(search)

            # Refill freed slskd slots before potentially expensive matching and enqueueing.
            fill_search_slots()

            for search in completed:
                download = search.queue_download()
                if download:
                    downloads.append(download)

            if not completed and pending:
                time.sleep(1)

        return downloads

    def filter_queued(self) -> Self:
        """Return albums that are not already in Lidarr's download queue."""
        if not self:
            return self

        current_queue = []
        total_queued = 1
        page = 1

        try:
            while len(current_queue) < total_queued:
                queue_page = lidarr.get_queue(
                    page=page,
                    sort_key="albums.title",
                    sort_dir="ascending",
                )
                page_records = queue_page["records"]
                total_queued = queue_page["totalRecords"]

                if not page_records:
                    if len(current_queue) < total_queued:
                        logger.warning("Lidarr returned an empty queue page before "
                                       "totalRecords was reached, so the queue was not filtered")
                        return self
                    break

                current_queue.extend(page_records)
                page += 1
        except PyarrError as ex:
            logger.error(f"Failed to get queue details, so the queue was not filtered: {ex}")
            return self

        queued_ids = set()

        for record in current_queue:
            record_ids = {release["albumId"] for release in record.get("releases", []) if "albumId" in release}
            if "albumId" in record:
                record_ids.add(record["albumId"])

            if not record_ids:
                logger.warning(f"Ignoring queue entry without albumId: {record.keys()}")

            queued_ids.update(record_ids)

        not_queued = []
        for album in self:
            if album.id in queued_ids:
                logger.info(f"Skipping album '{album.title}' because it's already in download queue")
            else:
                not_queued.append(album)

        return type(self)(not_queued)


@dataclass
class AlbumDownload:
    """An album's enqueued downloads, tracked from enqueue through Lidarr import.

    Built once by AlbumSearch.queue_download(), then mutated in place by monitor_downloads() and
    process_completed_album()/trigger_lidarr_import() as the download/import progresses.
    Identity fields (id, artist, title, ...) are read from `album` instead of being
    duplicated here.
    """

    SUCCESS = "Completed, Succeeded"
    REMOTE = "Queued, Remotely"
    album: WantedAlbum
    files: list[dict] = field(default_factory=list)
    candidates: deque[DownloadCandidate] = field(default_factory=deque)
    candidate: DownloadCandidate | None = None
    count_start: float = field(default_factory=time.monotonic)

    @property
    def artist(self) -> str:
        return self.album.artist

    @property
    def title(self) -> str:
        return self.album.title

    @property
    def year(self) -> str:
        return self.album.year

    @property
    def cfg(self) -> AppConfig:
        return self.album.cfg

    def enqueue_next(self) -> DownloadCandidate | None:
        """Enqueue and return the next available candidate."""
        self.candidate = None

        while self.candidates:
            candidate = self.candidates.popleft()
            downloads, cleanup_ok = self.enqueue_candidate(candidate)
            if not cleanup_ok:
                self.candidate = candidate
                self.files = downloads or []
                self.candidates.clear()
                self.album.failure = "Failed to clean up failed enqueue"
                logger.error(
                    f"Refusing further fallback for {self.artist} - {self.title}: "
                    "a partial candidate could not be fully removed"
                )
                return None
            if not downloads:
                logger.info(
                    f"Stored candidate unavailable for {self.artist} - {self.title}; "
                    "continuing to the next candidate"
                )
                continue

            self.files = downloads
            self.candidate = candidate
            self.count_start = time.monotonic()
            logger.info(f"Selected candidate for {self.artist} - {self.title}: {candidate.summary}")
            return candidate

        return None

    def enqueue_candidate(self, candidate: DownloadCandidate) -> tuple[list[dict] | None, bool]:
        """Enqueue every source in a candidate, removing partial attempts."""
        downloads = []
        for source in candidate.sources:
            source_downloads, accepted = self._enqueue_match(source)
            if not accepted:
                partial = downloads + (source_downloads or [])
                return partial, self.cancel_and_delete(partial)
            if not source_downloads:
                if downloads:
                    return None, self.cancel_and_delete(downloads)
                return None, True
            downloads.extend(source_downloads)
        return downloads, True

    def _enqueue_match(self, source: DownloadSource) -> tuple[list[dict] | None, bool]:
        """Enqueue an already validated album directory."""
        files = {file["filename"]: file.copy() for file in source.files}
        required_names = source.required_names

        for filename, file in files.items():
            file["required"] = filename in required_names
            file["filename"] = f"{source.file_dir}\\{filename}"

        try:
            downloads = slskd_do_enqueue(source.username, list(files.values()), source.file_dir)
        except Exception:
            logger.exception(f"Exception enqueueing {self.artist} - {self.title} from {source.username}")
            return None, True

        accepted = {file["filename"] for file in downloads or []}
        required = {f"{source.file_dir}\\{filename}" for filename in source.required_names}

        if downloads and required <= accepted:
            for file in downloads:
                if source.disk_no is not None:
                    file["disk_no"] = source.disk_no
                    file["disk_count"] = source.disk_count
            return downloads, True

        logger.info(f"Failed to enqueue {self.artist} - {self.title} from {source.username}")
        return downloads or None, not downloads

    def cancel_and_delete(self, files) -> bool:
        """Cancel downloads and remove their local files."""
        success = True
        for file in files:
            cancelled = False
            try:
                cancelled = bool(slskd.transfers.cancel_download(
                    username=file["username"], id=file["id"], remove=True
                ))
            except Exception:
                logger.warning(
                    f"Failed to cancel download {file['filename']} for {file['username']}",
                    exc_info=True,
                )

            if not cancelled and not self.transfer_absent(file):
                success = False

            filename = file["filename"].split("\\")[-1]
            folder = file["file_dir"].split("\\")[-1]
            delete_dir = safe_path(self.cfg.slskd.download_dir, folder)
            delete_file = safe_path(delete_dir, filename) if delete_dir else None
            if delete_file and os.path.isfile(delete_file) and not os.path.islink(delete_file):
                try:
                    os.remove(delete_file)
                except OSError:
                    success = False
                    logger.warning(f"Failed to remove download file {delete_file}", exc_info=True)
            if delete_dir and os.path.isdir(delete_dir) and not os.path.islink(delete_dir):
                try:
                    os.rmdir(delete_dir)
                except OSError:
                    pass

        return success

    @staticmethod
    def transfer_absent(file) -> bool:
        try:
            downloads = slskd.transfers.get_downloads(username=file["username"])
            return not any(
                str(transfer.get("id")) == str(file["id"]) for directory in downloads.get("directories", [])
                for transfer in directory.get("files", [])
            )
        except Exception:
            logger.warning(
                f"Failed to verify removal of download {file['filename']} for {file['username']}", exc_info=True)
            return False

    def process_completed_album(self) -> None:
        """Prepare a completed download and optionally import it into Lidarr."""
        if self.cfg.slskd.rename_download_folders:
            folder_name = sanitize_folder_name(f"{self.artist} - {self.title} ({self.year})")
        else:
            folder_name = self.files[0]["file_dir"].split("\\")[-1]

        staging_folder = safe_path(self.cfg.slskd.download_dir, folder_name)
        if staging_folder is None:
            logger.error("Refusing to use an invalid slskd download directory")
            self.album.failure = "Download processing failed"
            return

        import_folder = os.path.join(self.cfg.lidarr.download_dir, folder_name)
        source_dirs = {safe_path(self.cfg.slskd.download_dir, file["file_dir"].split("\\")[-1]) for file in self.files}
        if None in source_dirs:
            logger.error("Refusing to use an invalid slskd source directory")
            self.album.failure = "Download processing failed"
            return

        staging_folder_created = False
        if os.path.exists(staging_folder):
            if not os.path.isdir(staging_folder) or staging_folder not in source_dirs:
                logger.error(f"Refusing to reuse existing staging directory: {staging_folder}")
                self.album.failure = "Download processing failed"
                return
        else:
            try:
                os.mkdir(staging_folder)
                staging_folder_created = True
            except OSError:
                logger.exception(f"Failed to create staging directory: {staging_folder}")
                self.album.failure = "Download processing failed"
                return

        success, source_dirs = self.move_album_files(staging_folder)
        if not success:
            if staging_folder_created:
                try:
                    os.rmdir(staging_folder)
                except OSError:
                    logger.warning(f"Could not remove temp import directory {staging_folder}")

            self.album.failure = "Download processing failed"
            return

        for source_dir in source_dirs:
            if source_dir == staging_folder:
                continue
            try:
                os.rmdir(source_dir)
            except OSError:
                logger.warning(f"Skipping removal of {source_dir} because it is not empty")

        if self.cfg.lidarr.disable_sync:
            logger.info(f"Sync disabled. Skipping Lidarr import of {self.artist} - {self.title}")
            self.album.state.grabbed = True
            return

        logger.info(f"Attempting Lidarr import of {self.artist} - {self.title}")
        self.tag_album_files()
        self.trigger_lidarr_import(import_folder)

    def tag_album_files(self) -> None:
        """Add album metadata and optional multi-disc tags to downloaded audio files."""
        for file in self.files:
            try:
                song = music_tag.load_file(file["import_path"])
                if song is None:
                    continue

                if "disk_no" in file:
                    song["discnumber"] = file["disk_no"]
                    song["totaldiscs"] = file["disk_count"]

                song["albumartist"] = self.artist
                song["album"] = self.title
                song.save()
            except NotImplementedError:
                continue
            except Exception:
                logger.exception(f"Error tagging file: {file['import_path']}")

    def move_album_files(self, staging_folder: str):
        """Move/rename each file into the shared import folder, tracking source folders to clean up.

        Returns (success, rm_dirs). On failure, already-moved files are rolled back to their
        original location and the caller is responsible for removing the (now-empty) import folder.
        """
        rm_dirs = set()
        moved_files_history = []
        move_plan = []
        for file in self.files:
            file_folder = file["file_dir"].split("\\")[-1]
            filename = file["filename"].split("\\")[-1]
            src_file = safe_path(self.cfg.slskd.download_dir, file_folder, filename)
            if src_file is None:
                logger.error("Refusing to move a file from an invalid slskd download path")
                return False, rm_dirs
            src_folder = os.path.dirname(src_file)
            rm_dirs.add(src_folder)
            if "disk_no" in file and "disk_count" in file and file["disk_count"] > 1:
                filename = f"Disk {file['disk_no']} - {filename}"
            dst_file = safe_path(staging_folder, filename)
            if dst_file is None:
                logger.error("Refusing to move a file to an invalid staging path")
                return False, rm_dirs
            move_plan.append((file, src_file, dst_file))

        for file, src_file, dst_file in move_plan:
            file["import_path"] = dst_file
            if os.path.abspath(src_file) == os.path.abspath(dst_file):
                continue
            try:
                if os.path.lexists(dst_file):
                    raise FileExistsError(f"Destination already exists: {dst_file}")
                shutil.move(src_file, dst_file)
                moved_files_history.append((src_file, dst_file))
            except Exception:
                logger.exception(f"Failed to move: {file['filename']} to temp location for import into Lidarr. Rolling back...")
                for src, dst in reversed(moved_files_history):
                    try:
                        shutil.move(dst, src)
                    except Exception:
                        logger.exception(f"Critical failure during rollback: could not move {dst} back to {src}")
                return False, rm_dirs
        return True, rm_dirs

    def refresh_download_status(self, statuses: dict[tuple[str, str | int], dict]) -> bool:
        """Attach transfer statuses from a shared snapshot."""
        ok = True

        for file in self.files:
            status = statuses.get((file["username"], file["id"]))
            state = status.get("state") if isinstance(status, dict) else None
            file["status"] = status if isinstance(state, str) and state else None

            if file["status"] is None and file.get("required", True):
                logger.warning(f"Missing download status for {file['filename']}")
                ok = False

        return ok

    def discard_incomplete_optional_files(self) -> None:
        """Cancel optional transfers that did not complete before required files finished."""
        optional_files = [
            file for file in self.files if not file.get("required", True) and (file.get("status") or {}).get("state") != self.SUCCESS
        ]
        if optional_files:
            self.cancel_and_delete(optional_files)
            self.files = [file for file in self.files if file not in optional_files]

    def fail(self, reason: str) -> bool:
        """Cancel this album, record its failure, and mark it terminal."""
        self.cancel_and_delete(self.files)
        self.album.failure = reason
        logger.info(f"{reason}: {self.artist} - {self.title}")
        return True

    def retry_or_fail(self, reason: str) -> bool:
        if self.cfg.slskd.requeue_failed_downloads:
            return self.try_next_candidate(reason)
        return self.fail(reason)

    def try_next_candidate(self, reason: str) -> bool:
        """Replace the failed album attempt, returning True when no candidate remains."""
        failed_location = self.candidate.location if self.candidate else "unknown source"

        if not self.cancel_and_delete(self.files):
            self.candidates.clear()
            self.album.failure = "Failed to remove previous album attempt"
            active_transfers = ", ".join(f"{file['username']}:{file['id']}" for file in self.files)
            logger.error("Refusing fallback for %s - %s: candidate removal failed; transfers=%s", self.artist, self.title, active_transfers)
            return True

        if candidate := self.enqueue_next():
            logger.info(
                f"Replaced failed candidate ({failed_location}) for {self.artist} - {self.title} "
                f"after {reason}: {candidate.summary}"
            )
            return False

        if self.album.failure == "Failed to clean up failed enqueue":
            return True

        self.files = []
        self.album.failure = reason
        logger.info(f"{reason}: {self.artist} - {self.title}")
        return True

    def poll(self, statuses: dict[tuple[str, int], dict]) -> bool:
        """Interpret one shared transfer snapshot, returning True when terminal."""
        elapsed = time.monotonic() - self.count_start

        if not self.refresh_download_status(statuses):
            if elapsed >= self.cfg.slskd.stalled_timeout:
                return self.retry_or_fail("Timeout waiting for download status")
            return False

        states = [(file.get("status") or {}).get("state") for file in self.files if file.get("required", True)]

        if all(state == self.SUCCESS for state in states):
            logger.info(f"Completed download of {self.artist} - {self.title}")
            self.discard_incomplete_optional_files()
            self.candidates.clear()
            self.process_completed_album()
            return True

        if elapsed >= self.cfg.slskd.stalled_timeout:
            return self.retry_or_fail("Timeout waiting for download")

        if all(state == self.REMOTE for state in states) and elapsed >= self.cfg.slskd.remote_queue_timeout:
            return self.retry_or_fail("Remote queue timeout")

        if any(state and state.startswith("Completed,") and state != self.SUCCESS for state in states):
            return self.retry_or_fail("Failed grab")

        return False

    def move_failed_import(self, src_path) -> str | None:
        """Move a failed Lidarr import's folder into `cfg.slskd.failed_imports_dir`, avoiding name clashes."""
        failed_imports_dir = self.cfg.slskd.failed_imports_dir

        os.makedirs(failed_imports_dir, exist_ok=True)

        folder_name = os.path.basename(os.path.normpath(src_path))
        folder_path = safe_path(self.cfg.slskd.download_dir, folder_name)
        if folder_path is None:
            logger.error("Refusing to move a failed import from an invalid slskd download path")
            return None
        target_path = os.path.join(failed_imports_dir, folder_name)

        counter = 1
        while os.path.exists(target_path):
            target_path = os.path.join(failed_imports_dir, f"{folder_name}_{counter}")
            counter += 1

        if os.path.exists(folder_path):
            shutil.move(folder_path, target_path)
            logger.info(f"Failed import moved to: {target_path}")
            return os.path.abspath(target_path)

        logger.warning(f"Failed import source folder not found: {folder_path}")
        return None

    def trigger_lidarr_import(self, import_folder: str) -> None:
        """Trigger a Lidarr scan and record its final result."""
        command = lidarr.post_command(
            name="DownloadedAlbumsScan",
            path=import_folder,
        )
        logger.info(f"Starting Lidarr import for: {self.title} ID: {command['id']}")

        deadline = time.monotonic() + self.cfg.lidarr.import_timeout
        while True:
            current_task = lidarr.get_command(command["id"])

            if not isinstance(current_task, dict):
                logger.error(f"Invalid Lidarr command response: {current_task!r}")
                self.album.failure = "Invalid Lidarr import response"
                return

            if current_task.get("status") in ("completed", "failed"):
                break
            if time.monotonic() >= deadline:
                logger.error(f"Timed out waiting for Lidarr import of {self.artist} - {self.title}")
                self.album.state.import_pending = True
                return
            time.sleep(2)

        path = current_task.get("body", {}).get("path") or import_folder
        logger.info(f"{current_task.get('commandName', 'Lidarr command')} {current_task.get('message', '')} from: {path}")
        failed = current_task.get("status") == "failed" or current_task.get("result") == "unsuccessful"
        if not failed:
            self.album.state.import_failed = False
            return

        self.album.failure = "Lidarr import failed"
        try:
            self.move_failed_import(path)
        except Exception:
            logger.exception("Error moving failed Lidarr import")

        if self.cfg.lidarr.failed_import_denylist:
            self.album.state.import_failed = True
            logger.info(
                f"Added to failed import denylist: "
                f"{self.artist} - {self.title} (ID: {self.album.id})"
            )


class DownloadBatch(list[AlbumDownload]):
    """Active album downloads monitored as one batch."""

    def refresh_download_status(self) -> dict[tuple[str, int], dict]:
        """Fetch each active user's downloads and index them by username and ID."""
        statuses = {}
        usernames = {file["username"] for download in self for file in download.files}

        for username in usernames:
            try:
                downloads = slskd.transfers.get_downloads(username=username)
                for directory in downloads["directories"]:
                    for file in directory["files"]:
                        if "id" in file:
                            statuses[(username, file["id"])] = file
            except Exception:
                logger.exception(f"Error getting downloads for {username}")

        return statuses

    def monitor_downloads(self) -> None:
        """Poll active albums until every album reaches a terminal state."""
        while self:
            statuses = self.refresh_download_status()
            for download in self.copy():
                if download.poll(statuses):
                    self.remove(download)
                    break
            else:
                time.sleep(5)


def sanitize_folder_name(folder_name):
    valid_characters = re.sub(r'[<>:."/\\|?*]', "", folder_name)
    return valid_characters.strip()


def safe_path(download_root: str, *parts: str) -> str | None:
    """Return a validated child path, or None for unsafe components."""
    if not parts or any(not part or part in (".", "..") or os.path.isabs(part) or "/" in part or "\\" in part for part in parts):
        return None

    try:
        root_path = os.path.realpath(download_root)
        candidate_path = os.path.realpath(os.path.join(root_path, *parts))
        return candidate_path if os.path.commonpath((root_path, candidate_path)) == root_path else None
    except ValueError:
        return None


def verify_filetype(file, allowed_filetype):
    """Return whether a search-result file matches an allowed filetype and quality."""
    extension, _, attributes = allowed_filetype.partition(" ")
    current_extension = file["filename"].rsplit(".", 1)[-1]

    if current_extension.lower() != extension.lower():
        return False
    if not attributes:
        return True

    if "/" not in attributes:
        bitrate = file.get("bitRate")
        return bool(bitrate) and str(bitrate) == attributes

    bitdepth, samplerate = attributes.split("/", 1)
    try:
        samplerate = str(int(float(samplerate) * 1000))
    except ValueError:
        logger.warning("Invalid samplerate in allowed filetype")
        return False

    file_bitdepth = file.get("bitDepth")
    file_samplerate = file.get("sampleRate")
    if not file_bitdepth or not file_samplerate:
        return False

    return str(file_bitdepth) == bitdepth and str(file_samplerate) == samplerate


def slskd_do_enqueue(username, files, file_dir):
    """Enqueue files for download from a user and return the ones slskd accepted.

    Each returned file dict is annotated with the tracking details (id, file_dir, username,
    size) needed to poll its download status later.
    """
    try:
        before = slskd.transfers.get_downloads(username=username)
    except HTTPError as ex:
        if ex.response is None or ex.response.status_code != 404:
            logger.warning(f"Failed to snapshot existing downloads for {username} before enqueue", exc_info=True)
            return None
        before = {"directories": []}
    except Exception:
        logger.warning(f"Failed to snapshot existing downloads for {username} before enqueue", exc_info=True)
        return None

    before_directory = next((d for d in before["directories"] if d["directory"] == file_dir), {"files": []})
    existing_ids = {file["id"] for file in before_directory["files"] if "id" in file}

    try:
        enqueue_files = [{"filename": file["filename"], "size": file["size"]} for file in files]
        enqueue = slskd.transfers.enqueue(username=username, files=enqueue_files)
    except Exception:
        logger.debug("Enqueue failed", exc_info=True)
        return None
    if not enqueue:
        return None
    if isinstance(enqueue, dict):
        enqueue = enqueue.get("files", [enqueue])
    if not isinstance(enqueue, list):
        enqueue = []

    accepted_ids = {record["filename"]: record["id"] for record in enqueue if isinstance(record, dict) and "filename" in record and "id" in record}
    required_names = {file["filename"] for file in files if file.get("required", True)}
    deadline = time.monotonic() + 30  # add additional 30 seconds to wait for slskd to update the download status

    while not required_names.issubset(accepted_ids) and time.monotonic() < deadline:
        try:
            download_list = slskd.transfers.get_downloads(username=username)
            directory = next((d for d in download_list["directories"] if d["directory"] == file_dir), None)
            if directory is not None:
                accepted_ids.update({file["filename"]: file["id"] for file in directory["files"] if "id" in file and file["id"] not in existing_ids})
        except Exception:
            logger.warning(f"Failed to get download status for {username} after enqueue", exc_info=True)
        if not required_names.issubset(accepted_ids):
            time.sleep(1)

    return [
        {
            "filename": file["filename"],
            "id": accepted_ids[file["filename"]],
            "file_dir": file_dir,
            "username": username,
            "size": file["size"],
            "required": file.get("required", True),
        } for file in files if file["filename"] in accepted_ids
    ]


def is_docker():
    return os.getenv("IN_DOCKER") is not None


def acquire_lock_file(path: str):
    """Acquire an OS lock that is released automatically if the process crashes."""
    lock_file = open(path, "a")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        logger.info("Soulseekarr instance is already running.")
        return None
    return lock_file


def setup_logging(config: dict, var_dir: str) -> None:
    """Configure console and optional rotating-file logging."""
    from logging.handlers import RotatingFileHandler

    log_config = DEFAULT_LOGGING | (config.get("logging") or {})
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_file_path = None

    if log_config["log_to_file"]:
        log_file_path = os.path.join(
            var_dir,
            log_config["log_file"],
        )
        handlers.append(RotatingFileHandler(
            log_file_path,
            maxBytes=int(log_config["max_bytes"]),
            backupCount=int(log_config["backup_count"]),
        ))

    logging.basicConfig(
        level=log_config["level"],
        format=log_config["format"],
        datefmt=log_config["datefmt"],
        handlers=handlers,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if log_file_path:
        logger.info(f"Logging to file: {log_file_path}")


def env_override(section: str, key: str, value):
    """Override a resolved config value with an env var named "<SECTION>_<KEY>".

    Matches the YAML path (e.g. lidarr.api_key -> LIDARR_API_KEY), coercing the env var to
    match the existing value's type.
    """
    env_name = f"{section}_{key}".upper()
    raw = os.environ.get(env_name)
    if raw is None:
        return value
    if isinstance(value, bool):
        normalized = raw.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{env_name} must be a boolean, got {raw!r}")
    if isinstance(value, list):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(value, (int, float)):
        try:
            return type(value)(raw)
        except ValueError as ex:
            raise ValueError(
                f"{env_name} must be a {type(value).__name__}, got {raw!r}"
            ) from ex
    return raw


def main():
    """Parse CLI arguments, resolve configuration, then run Soulseekarr once or on a loop."""
    global lidarr, slskd
    parser = argparse.ArgumentParser(
        description="""Soulseekarr reads all of your "wanted" albums/artists from Lidarr and downloads them using Slskd"""
    )
    default_data_directory = "/data" if is_docker() else os.getcwd()
    parser.add_argument(
        "-c",
        "--config-dir",
        default=default_data_directory,
        const=default_data_directory,
        nargs="?",
        help="Config directory (default: %(default)s)",
    )
    parser.add_argument(
        "-v",
        "--var-dir",
        default=default_data_directory,
        const=default_data_directory,
        nargs="?",
        help="Var directory (default: %(default)s)",
    )
    parser.add_argument("--no-lock-file", action="store_false", dest="lock_file", help="Disable lock file creation")
    parser.add_argument(
        "--interval", type=int, default=None, help="Seconds to wait between runs, overriding LOOP_INTERVAL and config.yml. Use 0 to run once."
    )
    args = parser.parse_args()

    lock_file_path = os.path.join(args.var_dir, ".soulseekarr.lock")
    config_file_path = os.path.join(args.config_dir, "config.yml")
    lock_file = None
    try:
        if not is_docker() and args.lock_file:
            lock_file = acquire_lock_file(lock_file_path)
            if lock_file is None:
                return
        if not os.path.exists(config_file_path) and migrate_soularr_ini_config(args.config_dir):
            return
        if not os.path.exists(config_file_path):
            logger.error(
                'Config file does not exist! Please mount "/data" and place your "config.yml" file there.' if is_docker(
                ) else "Config file does not exist! Please place it in the working directory."
            )
            return

        with open(config_file_path, "r") as config_file:
            raw_config = expand_env_vars(yaml.safe_load(config_file) or {})
        setup_logging(raw_config, args.var_dir)
        cfg = AppConfig.from_yaml(raw_config, args)
        interval = cfg.interval
        slskd = slskd_api.SlskdClient(host=cfg.slskd.host_url, api_key=cfg.slskd.api_key, url_base=cfg.slskd.url_base)
        lidarr = LidarrAPI(urljoin(f"{cfg.lidarr.host_url.rstrip('/')}/", cfg.lidarr.url_base.strip("/")), cfg.lidarr.api_key)
        source_configs = {source: AppConfig.from_yaml(raw_config, args, source=source) for source in cfg.lidarr.sources}
        WantedAlbum._states.clear()
        wanted_backlogs = {source: WantedQueue() for source in source_configs}

        while True:
            wanted_albums = WantedQueue()
            for source, config in source_configs.items():
                logger.info(f"Getting wanted albums from '{source}' list")
                albums = wanted_backlogs[source].next_batch(config)
                logger.info(f"Fetched {len(albums)} albums from '{source}' list that aren't on the deny list and/or blacklisted.")
                wanted_albums.extend(albums)

            wanted_albums = wanted_albums.deduplicate()

            if not wanted_albums:
                logger.info("No wanted albums available.")
                if interval <= 0:
                    break
                time.sleep(interval)
                continue

            logger.info(f"Total wanted albums to process: {len(wanted_albums)}")

            if cfg.lidarr.shuffle_all:
                logger.info("shuffle_all: True (shuffling all wanted albums before processing)")
                wanted_albums.shuffle()

            try:
                total_failed = wanted_albums.grab_most_wanted(monitor_downloads=cfg.slskd.monitor_downloads)
                if total_failed == 0:
                    logger.info("Soulseekarr finished.")
                else:
                    logger.info(f"{total_failed}: releases failed to find a match in the search results and are still wanted.")
                if cfg.slskd.remove_completed_downloads:
                    slskd.transfers.remove_completed_downloads()
            except Exception:
                logger.exception("Fatal error!")
                return 1

            if interval > 0:
                time.sleep(interval)
            else:
                break
    finally:
        if lock_file is not None:
            lock_file.close()


if __name__ == "__main__":
    sys.exit(main())
