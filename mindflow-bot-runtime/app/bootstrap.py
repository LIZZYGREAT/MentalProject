"""Shared construction of participant-bound MindFlow business services."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.tool_registry import ToolRegistry
from app.config import Settings
from app.db import Database
from app.integrations.feishu.calendar import CalendarService
from app.integrations.feishu.oauth import DeviceFlowService, FeishuOAuthClient
from app.repositories import (
    AgentRunRepository,
    CalendarSnapshotRepository,
    ConversationRepository,
    EventSemanticCacheRepository,
    ForecastSnapshotRepository,
    ObservationRepository,
    ParticipantRepository,
    PredictionRepository,
    ProfileRepository,
    WarningScheduleRepository,
)
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.prediction_service import PredictionService
from app.services.token_service import (
    TokenEncryptionService,
    TokenRefreshService,
    TokenRepository,
)
from app.tools.care import CareTools
from mindflow_core.assessment import AssessmentModel
from services.event_semantics import OpenAICompatibleSemanticClient


@dataclass(frozen=True)
class BusinessServices:
    profiles: ProfileRepository
    observations: ObservationRepository
    predictions: PredictionRepository
    conversations: ConversationRepository
    token_repository: TokenRepository
    calendar: CalendarService
    prediction_service: PredictionService
    forecast_coordinator: ForecastCoordinator
    warning_schedules: WarningScheduleRepository
    registry: ToolRegistry
    device_flows: DeviceFlowService


def build_business_services(
    database: Database, settings: Settings, runs: AgentRunRepository
) -> BusinessServices:
    profiles = ProfileRepository(database)
    observations = ObservationRepository(database)
    predictions = PredictionRepository(database)
    conversations = ConversationRepository(database)
    encryption = TokenEncryptionService(settings.token_encryption_key)
    token_repository = TokenRepository(database, encryption)
    oauth = FeishuOAuthClient(settings.feishu_app_id, settings.feishu_app_secret)
    device_flows = DeviceFlowService(
        database, encryption, token_repository, oauth
    )
    refresh = TokenRefreshService(database, encryption, oauth.refresh_token)
    calendar = CalendarService(refresh)
    prediction_service = PredictionService(AssessmentModel(), predictions)
    semantic_client = None
    if settings.semantic_api_enabled and settings.deepseek_api_key:
        semantic_client = OpenAICompatibleSemanticClient(
            settings.semantic_api_url, settings.deepseek_api_key,
            settings.semantic_api_model, timeout=settings.semantic_api_timeout_seconds,
            provider="deepseek",
        )
    semantic_preprocessor = EventSemanticPreprocessor(
        EventSemanticCacheRepository(database), client=semantic_client,
        model=settings.semantic_api_model,
        batch_size=settings.semantic_batch_size,
        max_concurrency=settings.semantic_max_concurrency,
    )
    warning_schedules = WarningScheduleRepository(database)
    forecast_coordinator = ForecastCoordinator(
        participants=ParticipantRepository(database), profiles=profiles,
        observations=observations, calendar=calendar,
        calendar_snapshots=CalendarSnapshotRepository(database),
        semantics=semantic_preprocessor, prediction=prediction_service,
        forecasts=ForecastSnapshotRepository(database), warnings=warning_schedules,
        timezone_name=settings.timezone_name,
        materiality_threshold=settings.semantic_materiality_threshold,
    )
    registry = ToolRegistry(runs)
    CareTools(
        profiles,
        observations,
        predictions,
        prediction_service,
        calendar,
        token_repository,
        settings.timezone_name,
        forecast_coordinator,
    ).register(registry)
    return BusinessServices(
        profiles=profiles,
        observations=observations,
        predictions=predictions,
        conversations=conversations,
        token_repository=token_repository,
        calendar=calendar,
        prediction_service=prediction_service,
        forecast_coordinator=forecast_coordinator,
        warning_schedules=warning_schedules,
        registry=registry,
        device_flows=device_flows,
    )
