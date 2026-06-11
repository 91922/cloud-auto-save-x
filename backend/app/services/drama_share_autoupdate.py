from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extensions.runtime.adapter_registry import AdapterRegistry
from app.extensions.runtime.guessit_fallback import guessit_title_episode_numbers
from app.models.drive_account import DriveAccount
from app.schemas.task_browse import SharePreviewBatchIn, SharePreviewBatchItemOut
from app.services.resource_search import fetch_task_suggestions
from app.services.share_preview_batch import preview_share_batch
from app.services.tmdb_cache import get_tmdb_detail_cached

logger = logging.getLogger(__name__)

_RE_NON_WORD = re.compile(r"[\s\W_]+", re.UNICODE)
_RE_SEASON_EPISODE = re.compile(r"\bS(\d{1,3})E(\d{1,4})\b", re.IGNORECASE)
_RE_EPISODE_ONLY = re.compile(r"\b(?:EP(?:ISODE)?|第)\s*0*(\d{1,4})\s*(?:集)?\b", re.IGNORECASE)
_RE_YEAR_BRACKETS = re.compile(r"[\(\[（【]\s*(?:19|20)\d{2}\s*[\)\]）】]")
_RE_SOURCE_PREFIX = re.compile(r"^\s*(?:电视剧|剧集|连续剧|网剧|韩剧|日剧|美剧|英剧|台剧|泰剧|动漫|动画|番剧)\s*[:：]\s*", re.IGNORECASE)
_RE_NOISE_TOKEN = re.compile(
    r"\b(?:4k|8k|2160p|1080p|720p|bluray|bdrip|web-?dl|webrip|hdtv|x264|x265|h\.?264|h\.?265|hevc|aac|dts|uhd)\b",
    re.IGNORECASE,
)
_TITLE_SEGMENT_SEPARATORS = ("|", "｜", "/", "／")


@dataclass(slots=True)
class _TMDBContext:
    names: list[str]
    detail: dict[str, Any] | None


@dataclass(slots=True)
class _ResolvedCandidate:
    shareurl: str
    taskname: str
    datetime_value: str
    preview: SharePreviewBatchItemOut
    season: int
    episode: int
    size: int


@dataclass(slots=True)
class _PreparedSuggestion:
    suggestion: dict[str, Any]
    shareurl: str
    taskname: str
    datetime_value: str
    season: int | None
    episode: int | None


def _task_extra(task: Any) -> dict[str, Any]:
    raw = getattr(task, "extra_json", None)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_text(value: str | None) -> str:
    return _RE_NON_WORD.sub("", str(value or "").strip()).lower()


def _normalize_ascii_words(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _is_auto_update_enabled(task: Any) -> bool:
    extra = _task_extra(task)
    return bool(extra.get("auto_update_115_shareurl"))


def _pick_drive_type(db: Session, task: Any) -> str | None:
    account_name = str(getattr(task, "account_name", "") or "").strip()
    if account_name:
        row = db.execute(select(DriveAccount.drive_type).where(DriveAccount.name == account_name)).scalars().first()
        if row:
            dt = str(row or "").strip()
            return dt or None
    detected = AdapterRegistry.detect_drive_type(str(getattr(task, "shareurl", "") or ""))
    return str(detected or "").strip() or None


def is_115_auto_update_task(db: Session, task: Any, *, respect_toggle: bool = True) -> bool:
    if str(getattr(task, "task_type", "") or "") != "drama":
        return False
    if _pick_drive_type(db, task) != "115":
        return False
    if respect_toggle and not _is_auto_update_enabled(task):
        return False
    if str(getattr(task, "tmdb_media_type", "") or "").strip().lower() != "tv":
        return False
    try:
        return int(getattr(task, "tmdb_id", 0) or 0) > 0
    except Exception:
        return False


def _load_tmdb_context(db: Session, task: Any) -> _TMDBContext | None:
    try:
        tmdb_id = int(getattr(task, "tmdb_id", 0) or 0)
    except Exception:
        return None
    if tmdb_id <= 0:
        return None
    configured, detail, _update_weekdays, _episode_weekdays, _row = get_tmdb_detail_cached(db, media_type="tv", tmdb_id=tmdb_id)
    if not configured or not isinstance(detail, dict):
        return None
    names = _dedupe_preserve_order(
        [
            str(detail.get("name") or "").strip(),
            str(detail.get("original_name") or "").strip(),
        ]
    )
    if not names:
        return None
    return _TMDBContext(names=names, detail=detail)


def _rewrite_shareurl_with_fid(shareurl: str, fid: str | None) -> str:
    url = str(shareurl or "").strip()
    f = str(fid or "").strip()
    if not f or f == "0":
        return url.split("#")[0].strip()
    if f in url:
        return url
    return f"{url.split('#')[0].strip()}#/list/share/{f}"


def _extract_season_episode_from_title(title: str) -> tuple[int | None, int | None]:
    text = str(title or "").strip()
    if not text:
        return None, None
    if match := _RE_SEASON_EPISODE.search(text):
        return int(match.group(1)), int(match.group(2))
    if match := _RE_EPISODE_ONLY.search(text):
        return None, int(match.group(1))
    return None, None


def _resolve_title_progress(title: str, *, tv_seasons: list[dict[str, Any]] | None = None) -> tuple[int | None, int | None]:
    season, episode = _extract_season_episode_from_title(title)
    if season is not None and episode is not None:
        return season, episode
    guessed_season, guessed_episode = guessit_title_episode_numbers(
        title,
        tv_seasons=tv_seasons,
        trace_tag="shareurl_autoupdate",
    )
    if guessed_season is not None or guessed_episode is not None:
        return guessed_season, guessed_episode
    return season, episode


def _cleanup_title_subject(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _RE_SOURCE_PREFIX.sub("", text)
    text = _RE_YEAR_BRACKETS.sub(" ", text)
    text = _RE_SEASON_EPISODE.sub(" ", text)
    text = _RE_EPISODE_ONLY.sub(" ", text)
    text = _RE_NOISE_TOKEN.sub(" ", text)
    text = re.sub(r"[\(\[（【].*?[\)\]）】]", " ", text)
    text = re.sub(r"[:：\-_.,]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_title_subject_variants(value: str) -> list[str]:
    cleaned = _cleanup_title_subject(value)
    if not cleaned:
        return []
    variants = [cleaned]
    for separator in _TITLE_SEGMENT_SEPARATORS:
        if separator not in cleaned:
            continue
        parts = [x.strip() for x in cleaned.split(separator) if str(x or "").strip()]
        variants.extend(parts)
    return _dedupe_preserve_order(variants)


def _title_matches_tmdb_names(title: str, names: list[str]) -> bool:
    title_variants = _extract_title_subject_variants(title)
    if not title_variants:
        return False
    normalized_title_variants = {_normalize_text(x) for x in title_variants if _normalize_text(x)}
    normalized_ascii_variants = {_normalize_ascii_words(x) for x in title_variants if _normalize_ascii_words(x)}
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        name_variants = _extract_title_subject_variants(name) or [name]
        for item in name_variants:
            normalized_name = _normalize_text(item)
            if normalized_name and normalized_name in normalized_title_variants:
                return True
            normalized_ascii_name = _normalize_ascii_words(item)
            if normalized_ascii_name and normalized_ascii_name in normalized_ascii_variants:
                return True
    return False


def _preview_by_shareurl(db: Session, shareurls: list[str]) -> tuple[dict[str, SharePreviewBatchItemOut], bool]:
    if not shareurls:
        return {}, False
    out, changed = preview_share_batch(db, SharePreviewBatchIn(shareurls=shareurls, account_name=None))
    mapping: dict[str, SharePreviewBatchItemOut] = {}
    for row in out.items or []:
        url = str(getattr(row, "shareurl", "") or "").strip()
        if url:
            mapping[url] = row
    return mapping, bool(changed)


def _pick_int(value: Any) -> int | None:
    try:
        number = int(value)
    except Exception:
        return None
    return number if number > 0 else None


def _pick_datetime_value(value: str | None) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except Exception:
            continue
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        return 0


def _resolve_current_progress(current_preview: SharePreviewBatchItemOut | None) -> tuple[int | None, int | None]:
    latest = getattr(current_preview, "latest_video", None)
    if latest is None:
        return None, None
    season = _pick_int(getattr(latest, "season", None))
    episode = _pick_int(getattr(latest, "episode", None))
    return season, episode


def _resolve_candidate(
    *,
    suggestion: dict[str, Any],
    preview: SharePreviewBatchItemOut | None,
    fallback_season: int | None,
    tv_seasons: list[dict[str, Any]] | None = None,
) -> _ResolvedCandidate | None:
    if preview is None or not bool(getattr(preview, "ok", False)):
        return None
    latest = getattr(preview, "latest_video", None)
    title = str(suggestion.get("taskname") or "")
    title_season, title_episode = _resolve_title_progress(title, tv_seasons=tv_seasons)
    latest_season = _pick_int(getattr(latest, "season", None) if latest is not None else None)
    latest_episode = _pick_int(getattr(latest, "episode", None) if latest is not None else None)
    season = title_season or latest_season or fallback_season
    episode = title_episode or latest_episode
    if season is None or episode is None:
        return None
    size = _pick_int(getattr(latest, "size", None) if latest is not None else None) or 0
    return _ResolvedCandidate(
        shareurl=str(suggestion.get("shareurl") or "").strip(),
        taskname=title,
        datetime_value=str(suggestion.get("datetime") or "").strip(),
        preview=preview,
        season=int(season),
        episode=int(episode),
        size=int(size),
    )


def _prepare_suggestion(
    suggestion: dict[str, Any],
    *,
    tv_seasons: list[dict[str, Any]] | None = None,
) -> _PreparedSuggestion | None:
    shareurl = str(suggestion.get("shareurl") or "").strip()
    if not shareurl:
        return None
    title = str(suggestion.get("taskname") or suggestion.get("content") or "").strip()
    season, episode = _resolve_title_progress(title, tv_seasons=tv_seasons)
    return _PreparedSuggestion(
        suggestion=suggestion,
        shareurl=shareurl,
        taskname=title,
        datetime_value=str(suggestion.get("datetime") or "").strip(),
        season=season,
        episode=episode,
    )


def _pick_suggestions_for_preview(
    suggestions: list[dict[str, Any]],
    *,
    current_season: int | None,
    current_episode: int | None,
    tv_seasons: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    current_key = (int(current_season), int(current_episode)) if current_season is not None and current_episode is not None else None
    prepared_items: list[_PreparedSuggestion] = []
    for suggestion in suggestions:
        prepared = _prepare_suggestion(suggestion, tv_seasons=tv_seasons)
        if prepared is not None:
            prepared_items.append(prepared)

    parsed_candidates: list[_PreparedSuggestion] = []
    unknown_candidates: list[_PreparedSuggestion] = []
    for item in prepared_items:
        if item.season is not None and item.episode is not None:
            candidate_key = (int(item.season), int(item.episode))
            if current_key is not None and candidate_key <= current_key:
                continue
            parsed_candidates.append(item)
        else:
            unknown_candidates.append(item)

    if parsed_candidates:
        best_progress = max((int(item.season), int(item.episode)) for item in parsed_candidates if item.season is not None and item.episode is not None)
        selected = [item for item in parsed_candidates if (int(item.season), int(item.episode)) == best_progress]
        selected.sort(key=lambda item: _pick_datetime_value(item.datetime_value), reverse=True)
        return [item.suggestion for item in selected]

    unknown_candidates.sort(key=lambda item: _pick_datetime_value(item.datetime_value), reverse=True)
    return [item.suggestion for item in unknown_candidates[:3]]


def _search_candidates(db: Session, *, names: list[str]) -> tuple[list[dict[str, Any]], bool]:
    all_items: list[dict[str, Any]] = []
    db_changed = False
    for keyword in names:
        items, changed, _msg = fetch_task_suggestions(db, keyword=keyword, deep=1, drive_type="115")
        if changed:
            db_changed = True
        if isinstance(items, list):
            all_items.extend([x for x in items if isinstance(x, dict)])
    filtered: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in all_items:
        shareurl = str(item.get("shareurl") or "").strip()
        if not shareurl or shareurl in seen_urls:
            continue
        if AdapterRegistry.detect_drive_type(shareurl) != "115":
            continue
        title = str(item.get("taskname") or item.get("content") or "").strip()
        if not _title_matches_tmdb_names(title, names):
            continue
        seen_urls.add(shareurl)
        filtered.append(item)
        if len(filtered) >= 25:
            break
    return filtered, db_changed


def resolve_drama_shareurl_update(db: Session, task: Any, *, respect_toggle: bool = True) -> dict[str, Any]:
    if not is_115_auto_update_task(db, task, respect_toggle=respect_toggle):
        return {"checked": False, "updated": False, "reason": "not_applicable"}

    tmdb_context = _load_tmdb_context(db, task)
    if tmdb_context is None:
        return {"checked": False, "updated": False, "reason": "tmdb_context_missing"}

    old_shareurl = str(getattr(task, "shareurl", "") or "").strip()
    if not old_shareurl:
        return {"checked": False, "updated": False, "reason": "shareurl_empty"}

    current_preview_map, current_preview_changed = _preview_by_shareurl(db, [old_shareurl])
    current_preview = current_preview_map.get(old_shareurl)
    current_season, current_episode = _resolve_current_progress(current_preview)
    tv_seasons = tmdb_context.detail.get("seasons") if isinstance(tmdb_context.detail, dict) else None

    suggestions, suggestions_changed = _search_candidates(db, names=tmdb_context.names)
    if not suggestions:
        return {
            "checked": True,
            "updated": False,
            "reason": "no_candidates",
            "db_changed": bool(current_preview_changed or suggestions_changed),
        }

    preview_suggestions = _pick_suggestions_for_preview(
        suggestions,
        current_season=current_season,
        current_episode=current_episode,
        tv_seasons=tv_seasons if isinstance(tv_seasons, list) else None,
    )
    if not preview_suggestions:
        return {
            "checked": True,
            "updated": False,
            "reason": "no_better_candidate",
            "current_season": current_season,
            "current_episode": current_episode,
            "db_changed": bool(current_preview_changed or suggestions_changed),
        }

    preview_map, preview_changed = _preview_by_shareurl(
        db,
        [
            str(item.get("shareurl") or "").strip()
            for item in preview_suggestions
            if str(item.get("shareurl") or "").strip() != old_shareurl
        ],
    )

    best: _ResolvedCandidate | None = None
    best_key: tuple[int, int, int, int] | None = None
    current_key = (int(current_season), int(current_episode)) if current_season is not None and current_episode is not None else None

    for suggestion in preview_suggestions:
        shareurl = str(suggestion.get("shareurl") or "").strip()
        if not shareurl or shareurl == old_shareurl:
            continue
        candidate = _resolve_candidate(
            suggestion=suggestion,
            preview=preview_map.get(shareurl),
            fallback_season=current_season,
            tv_seasons=tv_seasons if isinstance(tv_seasons, list) else None,
        )
        if candidate is None:
            continue
        candidate_key = (candidate.season, candidate.episode)
        if current_key is not None and candidate_key <= current_key:
            continue
        sort_key = (
            int(candidate.season),
            int(candidate.episode),
            int(candidate.size),
            _pick_datetime_value(candidate.datetime_value),
        )
        if best_key is None or sort_key > best_key:
            best = candidate
            best_key = sort_key

    if best is None:
        return {
            "checked": True,
            "updated": False,
            "reason": "no_better_candidate",
            "current_season": current_season,
            "current_episode": current_episode,
            "db_changed": bool(current_preview_changed or suggestions_changed or preview_changed),
        }

    new_shareurl = _rewrite_shareurl_with_fid(
        best.shareurl,
        str(getattr(best.preview, "resolved_pdir_fid", None) or ""),
    )
    if not new_shareurl or new_shareurl == old_shareurl:
        return {
            "checked": True,
            "updated": False,
            "reason": "same_shareurl",
            "current_season": current_season,
            "current_episode": current_episode,
            "db_changed": bool(current_preview_changed or suggestions_changed or preview_changed),
        }

    task.shareurl = new_shareurl
    task.shareurl_ban = None
    db.flush()
    logger.info(
        "[shareurl_autoupdate] task_id=%s old=%s new=%s current=S%sE%s next=S%sE%s",
        int(getattr(task, "id", 0) or 0),
        old_shareurl,
        new_shareurl,
        str(current_season or ""),
        str(current_episode or ""),
        str(best.season),
        str(best.episode),
    )
    return {
        "checked": True,
        "updated": True,
        "old_shareurl": old_shareurl,
        "new_shareurl": new_shareurl,
        "current_season": current_season,
        "current_episode": current_episode,
        "season": best.season,
        "episode": best.episode,
        "size": best.size,
        "taskname": best.taskname,
        "db_changed": True,
    }
