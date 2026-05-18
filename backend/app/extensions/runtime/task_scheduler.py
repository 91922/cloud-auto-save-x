from __future__ import annotations

import os
import logging
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.settings import settings
from app.db.session import SessionLocal
from app.models.task import Task
from app.services.notifications.sender import send_runtime
from app.services.notifications.task_notify import DRAMA_NOTIFY_TITLE, build_task_section
from app.services.task_scheduler import get_or_create_task_scheduler_setting
from app.services.drama_share_repair import repair_banned_drama_tasks
from app.services.tmdb_cache import purge_cold_cache, refresh_expired_cache, refresh_linked_tasks
from app.services.tmdb_cache_scheduler import get_or_create_tmdb_cache_scheduler_setting
from app.services.drive_account_probe_scheduler import get_or_create_drive_account_probe_scheduler_setting
from app.services.drive_accounts import probe_drive_account
from app.models.drive_account import DriveAccount
from app.extensions.runtime.task_executor import TaskExecutor


logger = logging.getLogger(__name__)


class TaskSchedulerManager:
    def __init__(self):
        self.scheduler: BackgroundScheduler | None = None

    def start(self) -> None:
        if settings.environment == "test" or os.environ.get("PYTEST_CURRENT_TEST"):
            return
        if self.scheduler is not None:
            return
        self.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self.scheduler.start()
        self.reload()

    def shutdown(self) -> None:
        if self.scheduler is None:
            return
        self.scheduler.shutdown(wait=False)
        self.scheduler = None

    def reload(self) -> None:
        if self.scheduler is None:
            return
        with SessionLocal() as db:
            try:
                setting = get_or_create_task_scheduler_setting(db)
                db.commit()
                db.refresh(setting)
                self._apply_setting(setting)
            except OperationalError as e:
                logger.error(f"任务调度配置加载失败: {e}")
                return

            try:
                tmdb_cache_setting = get_or_create_tmdb_cache_scheduler_setting(db)
                db.commit()
                db.refresh(tmdb_cache_setting)
                self._apply_tmdb_cache_setting(tmdb_cache_setting)
            except OperationalError as e:
                logger.error(f"TMDB 缓存调度配置加载失败: {e}")
                return

            try:
                drive_probe_setting = get_or_create_drive_account_probe_scheduler_setting(db)
                db.commit()
                db.refresh(drive_probe_setting)
                self._apply_drive_account_probe_setting(drive_probe_setting)
            except OperationalError as e:
                logger.error(f"驱动账号探测调度配置加载失败: {e}")
                return

    def _apply_setting(self, setting: Any) -> None:
        if self.scheduler is None:
            return
        job_id = "drama_tasks"
        if not bool(setting.enabled):
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
            return
        trigger = CronTrigger.from_crontab(str(setting.crontab), timezone=str(setting.timezone or "Asia/Shanghai"))
        self.scheduler.add_job(
            run_drama_tasks,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

    def _apply_tmdb_cache_setting(self, setting: Any) -> None:
        if self.scheduler is None:
            return
        job_id = "tmdb_cache_refresh"
        if not bool(getattr(setting, "enabled", False)):
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
            return
        trigger = CronTrigger.from_crontab(str(setting.crontab), timezone=str(setting.timezone or "Asia/Shanghai"))
        self.scheduler.add_job(
            run_tmdb_cache_refresh,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

    def _apply_drive_account_probe_setting(self, setting: Any) -> None:
        if self.scheduler is None:
            return
        job_id = "drive_account_probe"
        if not bool(getattr(setting, "enabled", False)):
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
            return
        trigger = CronTrigger.from_crontab(str(setting.crontab), timezone=str(setting.timezone or "Asia/Shanghai"))
        self.scheduler.add_job(
            run_drive_account_probe,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )


def run_drama_tasks() -> None:
    with SessionLocal() as db:
        tasks = (
            db.execute(select(Task).where(Task.enabled.is_(True), Task.task_type == "drama").order_by(Task.id.asc()))
            .scalars()
            .all()
        )
        executor = TaskExecutor(db)
        pairs: list[tuple[Task, object]] = []
        for task in tasks:
            execution = executor.run_task(task)
            pairs.append((task, execution))
        db.commit()
        sections: list[str] = []
        for task, execution in pairs:
            try:
                section, should_notify = build_task_section(task, execution)
                if should_notify and section:
                    sections.append(section)
            except Exception:
                continue
        if sections:
            try:
                send_runtime(db, DRAMA_NOTIFY_TITLE, "\n\n".join(sections))
            except Exception:
                pass
        try:
            repair_banned_drama_tasks(db)
        except Exception:
            db.rollback()


def run_tmdb_cache_refresh() -> None:
    with SessionLocal() as db:
        setting = get_or_create_tmdb_cache_scheduler_setting(db)
        db.commit()
        db.refresh(setting)

        max_items = int(getattr(setting, "max_items_per_run", 200) or 200)
        only_linked = bool(getattr(setting, "only_refresh_linked_tasks", True))
        retention_days = int(getattr(setting, "retention_days", 60) or 60)

        try:
            if only_linked:
                refresh_linked_tasks(db, enabled_only=True, max_items=max_items, force=True)
            else:
                refresh_expired_cache(db, max_items=max_items, force=True)
            purge_cold_cache(db, retention_days=retention_days)
            db.commit()
        except Exception:
            db.rollback()


def run_drive_account_probe() -> None:
    with SessionLocal() as db:
        setting = get_or_create_drive_account_probe_scheduler_setting(db)
        db.commit()
        db.refresh(setting)
        enabled_only = bool(getattr(setting, "enabled_only", True))
        accounts = db.execute(select(DriveAccount).order_by(DriveAccount.is_default.desc(), DriveAccount.id.asc())).scalars().all()
        for account in accounts:
            if enabled_only and not bool(getattr(account, "enabled", False)):
                continue
            try:
                probe_drive_account(db, int(account.id))
            except Exception as exc:
                try:
                    account.runtime_status = "inactive"
                    account.last_error = str(exc)
                except Exception:
                    pass
                continue
        db.commit()


task_scheduler_manager = TaskSchedulerManager()
