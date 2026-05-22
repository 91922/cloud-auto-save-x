from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.extensions.runtime.guessit_fallback import guessit_episode_numbers
from app.models.task_savepath_snapshot import TaskSavepathSnapshot
from app.schemas.task import DramaUpdateProgressOut


_SH_TZ = ZoneInfo("Asia/Shanghai")
_VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".ts",
    ".m2ts",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
}


def _pick_tv_seasons(details: dict[str, Any] | None) -> list[dict] | None:
    if not isinstance(details, dict):
        return None
    raw = details.get("seasons")
    return raw if isinstance(raw, list) else None


def _parse_air_date(value: Any) -> date | None:
    try:
        s = str(value or "").strip()
        if not s:
            return None
        return date.fromisoformat(s)
    except Exception:
        return None


def resolve_tmdb_latest_aired_episode(details: dict[str, Any] | None) -> tuple[int | None, int | None, str | None]:
    if not isinstance(details, dict):
        return None, None, "TMDB 缓存缺失"

    next_ep = details.get("next_episode_to_air") if isinstance(details.get("next_episode_to_air"), dict) else None
    last_ep = details.get("last_episode_to_air") if isinstance(details.get("last_episode_to_air"), dict) else None

    picked = None
    if isinstance(next_ep, dict):
        ad = _parse_air_date(next_ep.get("air_date"))
        if ad is not None and ad <= datetime.now(_SH_TZ).date():
            picked = next_ep
    if picked is None:
        picked = last_ep if isinstance(last_ep, dict) else None
    if not isinstance(picked, dict):
        return None, None, "TMDB 缓存缺少 last/next episode"

    try:
        season = int(picked.get("season_number") or 0)
        episode = int(picked.get("episode_number") or 0)
    except Exception:
        return None, None, "TMDB last/next episode 数据异常"
    if season <= 0 or episode <= 0:
        return None, None, "TMDB last/next episode 数据缺失"
    return season, episode, None


def resolve_saved_latest_episode_for_season(
    *,
    snapshot: TaskSavepathSnapshot,
    tmdb_season: int,
    tv_seasons: list[dict] | None,
    max_items: int = 2000,
) -> tuple[int | None, int | None, str | None]:
    payload = None
    try:
        payload = json.loads(snapshot.files_json or "[]")
    except Exception:
        payload = None
    if not isinstance(payload, list):
        return None, None, "快照 files_json 无法解析"

    items = payload[: max(0, int(max_items))]
    truncated = len(payload) > len(items)

    best_episode = 0
    matched = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("file_name") or "").strip()
        if not name:
            continue
        ext = ""
        if "." in name:
            ext = "." + name.rsplit(".", 1)[-1].lower()
        if ext and ext not in _VIDEO_EXTS:
            continue
        season, episode = guessit_episode_numbers(name, tv_seasons=tv_seasons)
        if season is None or episode is None:
            continue
        if int(season) != int(tmdb_season):
            continue
        matched += 1
        if int(episode) > best_episode:
            best_episode = int(episode)

    if matched <= 0:
        return None, None, "当前季无匹配文件"
    reason = "文件过多已截断" if truncated else None
    return int(tmdb_season), int(best_episode), reason


def build_drama_update_progress(
    *,
    tmdb_details: dict[str, Any] | None,
    snapshot: TaskSavepathSnapshot | None,
) -> DramaUpdateProgressOut:
    tmdb_season, tmdb_episode, tmdb_reason = resolve_tmdb_latest_aired_episode(tmdb_details)
    if tmdb_season is None or tmdb_episode is None:
        return DramaUpdateProgressOut(
            available=False,
            tmdb_season=tmdb_season,
            tmdb_episode=tmdb_episode,
            snapshot_captured_at=snapshot.captured_at if snapshot is not None else None,
            reason=tmdb_reason or "TMDB 解析失败",
        )

    if snapshot is None:
        return DramaUpdateProgressOut(available=False, tmdb_season=tmdb_season, tmdb_episode=tmdb_episode, reason="无快照")

    tv_seasons = _pick_tv_seasons(tmdb_details)
    saved_season, saved_episode, saved_reason = resolve_saved_latest_episode_for_season(
        snapshot=snapshot,
        tmdb_season=int(tmdb_season),
        tv_seasons=tv_seasons,
    )

    if saved_season is None or saved_episode is None:
        return DramaUpdateProgressOut(
            available=False,
            tmdb_season=tmdb_season,
            tmdb_episode=tmdb_episode,
            saved_season=saved_season,
            saved_episode=saved_episode,
            snapshot_captured_at=snapshot.captured_at,
            reason=saved_reason or "快照解析失败",
        )

    behind = max(0, int(tmdb_episode) - int(saved_episode))
    is_latest = behind == 0
    return DramaUpdateProgressOut(
        available=True,
        tmdb_season=tmdb_season,
        tmdb_episode=tmdb_episode,
        saved_season=saved_season,
        saved_episode=saved_episode,
        behind_episodes=behind,
        is_latest=is_latest,
        snapshot_captured_at=snapshot.captured_at,
        reason=saved_reason,
    )
