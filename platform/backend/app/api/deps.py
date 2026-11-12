"""FastAPI dependency injection for service singletons."""
from __future__ import annotations

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.storage.s3 import S3Storage
from app.storage.strategy_store import StrategyStore
from app.services.code_service import CodeService
from app.services.deploy_service import DeployService
from app.services.log_service import LogService
from app.services.process_service import ProcessService
from app.services.scheduler_service import SchedulerService
from app.services.strategy_service import StrategyService


@lru_cache
def get_storage() -> S3Storage:
    return S3Storage(get_settings())


@lru_cache
def get_store() -> StrategyStore:
    return StrategyStore(get_storage())


@lru_cache
def get_strategy_service() -> StrategyService:
    return StrategyService(get_store(), CodeService(get_store()), get_settings())


@lru_cache
def get_code_service() -> CodeService:
    return CodeService(get_store(), get_settings())


@lru_cache
def get_deploy_service() -> DeployService:
    return DeployService(get_settings(), get_strategy_service())


@lru_cache
def get_scheduler_service() -> SchedulerService:
    return SchedulerService(get_settings(), get_strategy_service())


@lru_cache
def get_process_service() -> ProcessService:
    return ProcessService(get_settings(), get_strategy_service())


@lru_cache
def get_log_service() -> LogService:
    return LogService(get_settings(), get_process_service())
