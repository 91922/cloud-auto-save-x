from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.task_scheduler_setting import TaskSchedulerSetting


DEFAULT_TASK_SCHEDULER_SETTING_ID = 1


def get_or_create_task_scheduler_setting(db: Session) -> TaskSchedulerSetting:
    setting = (
        db.execute(select(TaskSchedulerSetting).where(TaskSchedulerSetting.id == DEFAULT_TASK_SCHEDULER_SETTING_ID))
        .scalars()
        .first()
    )
    if setting is not None:
        return setting
    setting = TaskSchedulerSetting(id=DEFAULT_TASK_SCHEDULER_SETTING_ID)
    db.add(setting)
    db.flush()
    return setting


def update_task_scheduler_setting(db: Session, *, enabled: bool | None = None, crontab: str | None = None, timezone: str | None = None) -> TaskSchedulerSetting:
    setting = get_or_create_task_scheduler_setting(db)
    if enabled is not None:
        setting.enabled = enabled
    if crontab is not None:
        setting.crontab = crontab
    if timezone is not None:
        setting.timezone = timezone
    db.flush()
    return setting

