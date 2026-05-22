from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.drive_account import DriveAccount
from app.models.task_savepath_snapshot import TaskSavepathSnapshot


def _normalize_savepath(savepath: str | None) -> str | None:
    raw = str(savepath or "").strip()
    if not raw:
        return None
    if raw in {"0", "/"}:
        return "/"
    if not raw.startswith("/"):
        raw = "/" + raw
    raw = raw.rstrip("/")
    return raw or "/"


def _bool_is_dir(payload: dict) -> bool:
    if payload.get("is_dir") is not None:
        return bool(payload.get("is_dir"))
    if payload.get("isdir") is not None:
        return str(payload.get("isdir")) in ("1", "true", "True")
    if payload.get("dir") is not None:
        return bool(payload.get("dir"))
    if payload.get("kind") in ("folder", "dir", "directory"):
        return True
    if payload.get("kind") in ("file",):
        return False
    if payload.get("type") in ("folder", "dir"):
        return True
    if payload.get("type") in ("file",):
        return False
    if payload.get("file_type") is not None:
        value = str(payload.get("file_type"))
        if value in ("0", "dir", "folder"):
            return True
        if value in ("1", "file"):
            return False
    return False


def _pick_name(payload: dict) -> str:
    return str(
        payload.get("file_name")
        or payload.get("server_filename")
        or payload.get("fileName")
        or payload.get("name")
        or payload.get("title")
        or payload.get("fid")
        or payload.get("fs_id")
        or ""
    )


def _pick_updated_at(payload: dict):
    return payload.get("updated_at") or payload.get("update_time") or payload.get("mtime") or payload.get("modified_at")


def _pick_size(payload: dict) -> int | None:
    if payload.get("size") is None:
        return None
    try:
        return int(payload.get("size"))
    except (TypeError, ValueError):
        return None


def _resolve_drive_account_id(db: Session, account_name: str) -> int | None:
    name = str(account_name or "").strip()
    if not name:
        return None
    return db.execute(select(DriveAccount.id).where(DriveAccount.name == name)).scalars().first()


def _resolve_dir_fid(adapter: Any, savepath: str) -> str | None:
    if savepath == "/":
        return "0"
    fid_list = adapter.get_fids([savepath]) or []
    match = None
    for item in fid_list:
        item_path = item.get("file_path") or item.get("path") or item.get("filePath")
        if str(item_path) == savepath:
            match = item
            break
    if match is None and fid_list:
        match = fid_list[0]
    fid = match.get("fid") if match else None
    return str(fid) if fid else None


def fetch_savepath_files(adapter: Any, savepath: str) -> list[dict[str, Any]]:
    fid = _resolve_dir_fid(adapter, savepath)
    if not fid:
        return []
    listing = adapter.ls_dir(str(fid), max_items=0) or {}
    raw_items = (((listing or {}).get("data") or {}).get("list")) or []
    result: list[dict[str, Any]] = []
    for raw in raw_items:
        if _bool_is_dir(raw):
            continue
        name = _pick_name(raw)
        if not name:
            continue
        size = _pick_size(raw)
        updated_at = _pick_updated_at(raw)
        result.append({"file_name": name, "size": size, "updated_at": updated_at})
    return result


def upsert_task_savepath_snapshot(
    db: Session,
    *,
    task_uid: str,
    task_execution_id: int | None,
    drive_account_id: int | None,
    savepath: str,
    files: list[dict[str, Any]],
) -> TaskSavepathSnapshot:
    files_json = json.dumps(files, ensure_ascii=False)
    file_count = len(files)
    total_size: int | None = 0
    for item in files:
        sz = item.get("size")
        if sz is None:
            total_size = None
            break
        try:
            total_size += int(sz)
        except (TypeError, ValueError):
            total_size = None
            break

    uid = str(task_uid or "").strip()
    if not uid:
        raise ValueError("task_uid required")
    row = db.execute(select(TaskSavepathSnapshot).where(TaskSavepathSnapshot.task_uid == uid)).scalars().first()
    if row is None:
        row = TaskSavepathSnapshot(
            task_uid=uid,
            task_execution_id=task_execution_id,
            drive_account_id=drive_account_id,
            savepath=savepath,
            files_json=files_json,
            file_count=file_count,
            total_size=total_size,
            captured_at=datetime.now(timezone.utc),
        )
        db.add(row)
        return row

    row.task_execution_id = task_execution_id
    row.drive_account_id = drive_account_id
    row.savepath = savepath
    row.files_json = files_json
    row.file_count = file_count
    row.total_size = total_size
    row.captured_at = datetime.now(timezone.utc)
    return row


def capture_and_upsert_snapshot(
    db: Session,
    *,
    task_uid: str,
    savepath: str | None,
    adapter: Any,
    account_name: str,
    emit_line: Any | None = None,
) -> TaskSavepathSnapshot | None:
    normalized = _normalize_savepath(savepath)
    if not normalized:
        return None

    drive_account_id = _resolve_drive_account_id(db, account_name)
    if drive_account_id is None:
        if emit_line:
            emit_line("保存路径快照: 跳过（无法解析 drive_account_id）")
        return None

    try:
        files = fetch_savepath_files(adapter, normalized)
    except Exception as e:
        if emit_line:
            emit_line(f"保存路径快照: 失败（{str(e)}）")
        return None

    if emit_line:
        emit_line(f"保存路径快照: OK（{len(files)} 个文件）")
    return upsert_task_savepath_snapshot(
        db,
        task_uid=task_uid,
        task_execution_id=None,
        drive_account_id=drive_account_id,
        savepath=normalized,
        files=files,
    )
