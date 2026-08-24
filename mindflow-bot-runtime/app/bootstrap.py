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
    LearnedProfileRepository,
    WarningScheduleRepository,
)
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.repositories_daily_review import (
    DailyReviewResponseRepository,
    DailyReviewScheduleRepository,
    RetrospectiveCurveRepository,
)
from app.services.daily_review_service import DailyReviewService
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.prediction_service import PredictionService
from app.services.pressure_curve_service import PressureCurveService
from app.services.presentation_service import PresentationOutbox
from app.services.card_action_service import CardActionService
from app.services.profile_calibration import ProfileCalibrationService
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
    forecast_snapshots: ForecastSnapshotRepository
    warning_schedules: WarningScheduleRepository
    semantic_preprocessor: EventSemanticPreprocessor
    registry: ToolRegistry
    device_flows: DeviceFlowService
    presentations: PresentationOutbox
    card_actions: CardActionService
    profile_calibration: ProfileCalibrationService
    pressure_curves: PressureCurveService
    daily_review_schedules: DailyReviewScheduleRepository
    daily_review_responses: DailyReviewResponseRepository
    retrospective_curves: RetrospectiveCurveRepository
    daily_reviews: DailyReviewService


def build_business_services(
    database: Database, settings: Settings, runs: AgentRunRepository
) -> BusinessServices:
    profiles = ProfileRepository(database)
    observations = ObservationRepository(database)
    predictions = PredictionRepository(database)
    conversations = ConversationRepository(database)
    encryption = TokenEncryptionService(settings.token_encryption_key)
    token_repository = TokenRepository(
        database, encryption, oauth_app_id=settings.feishu_calendar_app_id
    )
    oauth = FeishuOAuthClient(
        settings.feishu_calendar_app_id, settings.feishu_calendar_app_secret
    )
    device_flows = DeviceFlowService(
        database, encryption, token_repository, oauth
    )
    refresh = TokenRefreshService(
        database,
        encryption,
        oauth.refresh_token,
        expected_oauth_app_id=settings.feishu_calendar_app_id,
    )
    calendar = CalendarService(refresh, timezone_name=settings.timezone_name)
    presentations = PresentationOutbox()
    daily_review_schedules = DailyReviewScheduleRepository(database)
    daily_review_responses = DailyReviewResponseRepository(database)
    retrospective_curves = RetrospectiveCurveRepository(database)
    daily_reviews = DailyReviewService(
        daily_review_responses, daily_review_schedules, retrospective_curves,
        ForecastSnapshotRepository(database), observations, settings,
    )
    card_actions = CardActionService(
        observations, calendar, timezone_name=settings.timezone_name,
        daily_reviews=daily_reviews,
    )
    prediction_service = PredictionService(
        AssessmentModel(settings.timezone_name), predictions
    )
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
        max_concurrency=settings.semantic_max_concurrency,
    )
    warning_schedules = WarningScheduleRepository(database)
    forecast_snapshots = ForecastSnapshotRepository(database)
    learned_profiles = LearnedProfileRepository(database)
    profile_calibration = ProfileCalibrationService(
        observations, forecast_snapshots, learned_profiles, settings.timezone_name
    )
    forecast_coordinator = ForecastCoordinator(
        participants=ParticipantRepository(database), profiles=profiles,
        observations=observations, calendar=calendar,
        calendar_snapshots=CalendarSnapshotRepository(database),
        semantics=semantic_preprocessor, prediction=prediction_service,
        forecasts=forecast_snapshots, warnings=warning_schedules,
        timezone_name=settings.timezone_name,
        materiality_threshold=settings.semantic_materiality_threshold,
        warning_lead_minutes=settings.warning_lead_minutes,
        warning_late_grace_minutes=settings.warning_late_grace_minutes,
        warning_episode_drift_minutes=settings.warning_episode_drift_minutes,
        warning_max_daily_sends=settings.warning_max_daily_sends,
        warning_min_interval_minutes=settings.warning_min_interval_minutes,
        learned_profiles=learned_profiles,
        retrospective_curves=retrospective_curves,
    )
    pressure_curves = PressureCurveService(
        forecast_coordinator,
        timezone_name=settings.timezone_name,
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
        forecast_snapshots,
        presentations,
        learned_profiles=learned_profiles,
        pressure_curves=pressure_curves,
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
        forecast_snapshots=forecast_snapshots,
        warning_schedules=warning_schedules,
        semantic_preprocessor=semantic_preprocessor,
        registry=registry,
        device_flows=device_flows,
        presentations=presentations,
        card_actions=card_actions,
        profile_calibration=profile_calibration,
        pressure_curves=pressure_curves,
        daily_review_schedules=daily_review_schedules,
        daily_review_responses=daily_review_responses,
        retrospective_curves=retrospective_curves,
        daily_reviews=daily_reviews,
    )
