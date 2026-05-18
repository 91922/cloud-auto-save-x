from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.drive_account_probe_scheduler_setting import DriveAccountProbeSchedulerSetting


DEFAULT_DRIVE_ACCOUNT_PROBE_SCHEDULER_SETTING_ID = 1


def get_or_create_drive_account_probe_scheduler_setting(db: Session) -> DriveAccountProbeSchedulerSetting:
    item = db.get(DriveAccountProbeSchedulerSetting, DEFAULT_DRIVE_ACCOUNT_PROBE_SCHEDULER_SETTING_ID)
    if item is not None:
        return item
    item = DriveAccountProbeSchedulerSetting(id=DEFAULT_DRIVE_ACCOUNT_PROBE_SCHEDULER_SETTING_ID)
    db.add(item)
    db.flush()
    return item


def update_drive_account_probe_scheduler_setting(
    db: Session,
    *,
    enabled: bool | None = None,
    crontab: str | None = None,
    timezone: str | None = None,
    enabled_only: bool | None = None,
) -> DriveAccountProbeSchedulerSetting:
    item = get_or_create_drive_account_probe_scheduler_setting(db)
    if enabled is not None:
        item.enabled = enabled
    if crontab is not None:
        item.crontab = crontab
    if timezone is not None:
        item.timezone = timezone
    if enabled_only is not None:
        item.enabled_only = enabled_only
    db.flush()
    return item

