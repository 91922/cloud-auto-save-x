from __future__ import annotations

import os
import random
import time
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extensions.runtime.adapter_registry import AdapterRegistry
from app.models.drive_account import DriveAccount
from app.models.task import Task
from app.schemas.task_browse import SharePreviewBatchIn
from app.services.notifications.sender import send_runtime
from app.services.notifications.task_notify import DRAMA_NOTIFY_TITLE
from app.services.invalid_share_links import list_invalid_shareurls
from app.services.resource_search import fetch_task_suggestions
from app.services.share_preview_batch import preview_share_batch
from app.services.tmdb_cache import get_tmdb_detail_cached


def _debug_enabled() -> bool:
    return os.getenv("DEBUG", "0").strip().lower() in {"1", "true", "yes", "y", "on"}


def _clean_keyword(value: str) -> str:
    v = str(value or "").strip()
    if not v:
        return ""
    v = v.replace("《", "").replace("》", "")
    v = re.sub(r"[\[\]（）()]+", " ", v).strip()
    v = re.sub(r"[\s\-_\.]+", " ", v).strip()
    v = re.sub(r"(test|测试)\s*$", "", v, flags=re.IGNORECASE).strip()
    v = re.sub(r"\s{2,}", " ", v).strip()
    return v


def _pick_search_keywords(db: Session, task: Task) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        v = _clean_keyword(value)
        if len(v) < 2:
            return
        if v in seen:
            return
        seen.add(v)
        keywords.append(v)

    tmdb_media_type = str(getattr(task, "tmdb_media_type", "") or "").strip().lower()
    tmdb_id = getattr(task, "tmdb_id", None)
    if tmdb_media_type in {"tv", "movie"} and tmdb_id is not None:
        try:
            configured, detail, _weekdays, _row = get_tmdb_detail_cached(
                db,
                media_type=tmdb_media_type,  # type: ignore[arg-type]
                tmdb_id=int(tmdb_id),
                force_refresh=False,
            )
            if configured and isinstance(detail, dict):
                if tmdb_media_type == "tv":
                    add(str(detail.get("name") or ""))
                    add(str(detail.get("original_name") or ""))
                else:
                    add(str(detail.get("title") or ""))
                    add(str(detail.get("original_title") or ""))
        except Exception:
            pass

    add(str(getattr(task, "taskname", "") or ""))
    return keywords



def _rewrite_shareurl_with_fid(shareurl: str, fid: str | None) -> str:
    url = str(shareurl or "").strip()
    f = str(fid or "").strip()
    if not f or f == "0":
        head = url.split("#")[0].strip()
        return head
    if f in url:
        return url
    base = url.split("#")[0].strip()
    return f"{base}#/list/share/{f}"


def _pick_drive_type(db: Session, task: Task) -> str | None:
    account_name = str(getattr(task, "account_name", "") or "").strip()
    if account_name:
        row = db.execute(select(DriveAccount.drive_type).where(DriveAccount.name == account_name)).scalars().first()
        if row:
            dt = str(row or "").strip()
            return dt or None
    dt2 = AdapterRegistry.detect_drive_type(str(getattr(task, "shareurl", "") or ""))
    return None if not dt2 else str(dt2)


def repair_banned_drama_tasks(db: Session) -> dict:
    debug = _debug_enabled()
    rows = (
        db.execute(select(Task).where(Task.enabled.is_(True), Task.task_type == "drama").order_by(Task.id.asc()))
        .scalars()
        .all()
    )
    if debug:
        print(f"[repair] scanned_rows={len(rows)}")
    invalid_shareurls = list_invalid_shareurls(db, shareurls=[str(getattr(t, "shareurl", "") or "").strip() for t in rows])
    if debug:
        print(f"[repair] invalid_shareurls_matched={len(invalid_shareurls)}")
    targets: list[Task] = []
    for task in rows:
        task_id = int(getattr(task, "id", 0) or 0)
        taskname = str(getattr(task, "taskname", "") or "")
        ban = str(getattr(task, "shareurl_ban", "") or "").strip()
        shareurl = str(getattr(task, "shareurl", "") or "").strip()
        is_invalid = bool(shareurl and shareurl in invalid_shareurls)
        if not ban and not is_invalid:
            if debug:
                print(f"[repair] skip task_id={task_id} name={taskname} reason=no_ban_and_not_invalid")
            continue
        tmdb_media_type = str(getattr(task, "tmdb_media_type", "") or "").strip().lower()
        tmdb_id = getattr(task, "tmdb_id", None)
        if tmdb_media_type != "tv":
            if debug:
                print(
                    f"[repair] skip task_id={task_id} name={taskname} reason=tmdb_media_type_not_tv tmdb_media_type={tmdb_media_type}"
                )
            continue
        if tmdb_id is None:
            if debug:
                print(f"[repair] skip task_id={task_id} name={taskname} reason=tmdb_id_missing")
            continue
        if debug:
            print(
                f"[repair] target task_id={task_id} name={taskname} ban={ban!s} is_invalid={is_invalid} shareurl={shareurl}"
            )
        targets.append(task)

    if not targets:
        if debug:
            print("[repair] no_targets")
        return {"checked": 0, "repaired": 0, "items": []}

    repaired: list[dict] = []
    db_changed = False

    for task in targets:
        try:
            task_id = int(getattr(task, "id", 0) or 0)
            taskname = str(getattr(task, "taskname", "") or "")
            old_shareurl = str(getattr(task, "shareurl", "") or "").strip()
            drive_type = _pick_drive_type(db, task)
            if not drive_type:
                if debug:
                    print(f"[repair] skip task_id={task_id} name={taskname} reason=drive_type_unknown")
                continue
            if debug:
                print(f"[repair] task_id={task_id} name={taskname} drive_type={drive_type} old_shareurl={old_shareurl}")

            suggestions: list[dict] = []
            changed = False
            _msg = None
            for kw in _pick_search_keywords(db, task):
                suggestions, changed, _msg = fetch_task_suggestions(db, keyword=kw, deep=1)
                if debug:
                    print(f"[repair] task_id={task_id} keyword={kw} suggestions_count={len(suggestions or [])} msg={str(_msg or '')}")
                if suggestions:
                    break
            if changed:
                db_changed = True
            candidate_urls: list[str] = []
            seen: set[str] = set()
            for item in suggestions or []:
                url = str((item or {}).get("shareurl") or "").strip()
                if not url or url in seen:
                    continue
                if old_shareurl and url == old_shareurl:
                    if debug:
                        print(f"[repair] task_id={task_id} skip_candidate reason=same_as_old url={url}")
                    continue
                dt = AdapterRegistry.detect_drive_type(url)
                if dt != drive_type:
                    if debug:
                        print(
                            f"[repair] task_id={task_id} skip_candidate reason=drive_type_mismatch url={url} dt={dt} expected={drive_type}"
                        )
                    continue
                seen.add(url)
                candidate_urls.append(url)
                if len(candidate_urls) >= 30:
                    break
            if not candidate_urls:
                if debug:
                    print(f"[repair] task_id={task_id} no_candidate_urls")
                continue
            if debug:
                print(f"[repair] task_id={task_id} candidate_urls={candidate_urls}")

            batch_out, batch_changed = preview_share_batch(db, SharePreviewBatchIn(shareurls=candidate_urls, account_name=None))
            if batch_changed:
                db_changed = True
            if debug:
                ok_count = sum(1 for x in (batch_out.items or []) if bool(getattr(x, "ok", False)))
                print(f"[repair] task_id={task_id} preview_items={len(batch_out.items or [])} ok={ok_count}")
                for x in batch_out.items or []:
                    latest = getattr(x, "latest_video", None)
                    season = getattr(latest, "season", None) if latest is not None else None
                    episode = getattr(latest, "episode", None) if latest is not None else None
                    size = getattr(latest, "size", None) if latest is not None else None
                    print(
                        f"[repair] preview ok={bool(getattr(x,'ok',False))} url={str(getattr(x,'shareurl','') or '')} "
                        f"msg={str(getattr(x,'message', '') or '')} resolved_pdir_fid={str(getattr(x,'resolved_pdir_fid','') or '')} "
                        f"latest=S{season}E{episode} size={size}"
                    )

            best = None
            best_key = None
            for row in batch_out.items or []:
                if not bool(getattr(row, "ok", False)):
                    continue
                latest = getattr(row, "latest_video", None)
                if latest is None:
                    continue
                season = getattr(latest, "season", None)
                episode = getattr(latest, "episode", None)
                if season is None or episode is None:
                    continue
                size = getattr(latest, "size", None)
                key = (int(season), int(episode), int(size or 0))
                if best_key is None or key > best_key:
                    best_key = key
                    best = row

            if best is None or best_key is None:
                if debug:
                    print(f"[repair] task_id={task_id} no_best_match")
                continue

            latest = best.latest_video
            new_shareurl = _rewrite_shareurl_with_fid(str(best.shareurl or ""), str(getattr(best, "resolved_pdir_fid", None) or ""))
            if debug:
                print(f"[repair] task_id={task_id} picked best_key={best_key} new_shareurl={new_shareurl}")
            task.shareurl = new_shareurl
            task.shareurl_ban = None
            db.flush()
            db_changed = True

            repaired.append(
                {
                    "task_id": int(getattr(task, "id", 0) or 0),
                    "taskname": str(getattr(task, "taskname", "") or ""),
                    "drive_type": drive_type,
                    "old_shareurl": old_shareurl,
                    "new_shareurl": new_shareurl,
                    "season": getattr(latest, "season", None),
                    "episode": getattr(latest, "episode", None),
                    "size": getattr(latest, "size", None),
                }
            )
        except Exception:
            continue
        finally:
            time.sleep(random.uniform(0.2, 0.6))

    if not repaired:
        if db_changed:
            db.commit()
        if debug:
            print(f"[repair] done checked={len(targets)} repaired=0 db_changed={db_changed}")
        return {"checked": len(targets), "repaired": 0, "items": []}

    db.commit()
    if debug:
        print(f"[repair] done checked={len(targets)} repaired={len(repaired)}")
    lines: list[str] = []
    for item in repaired:
        s = item.get("season")
        e = item.get("episode")
        se = ""
        if s is not None and e is not None:
            se = f"S{int(s):02d}E{int(e):02d}"
        size = item.get("size")
        size_text = f" size={int(size)}" if size is not None else ""
        lines.append(f"✅《{item.get('taskname')}》 {se}{size_text}\n{item.get('new_shareurl')}")
    content = "🔧自动修复封禁分享链接：\n\n" + "\n\n".join(lines)
    try:
        send_runtime(db, DRAMA_NOTIFY_TITLE, content)
    except Exception:
        pass
    return {"checked": len(targets), "repaired": len(repaired), "items": repaired}
