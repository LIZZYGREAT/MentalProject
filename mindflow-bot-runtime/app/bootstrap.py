"""Shared construction of participant-bound MindFlow business services."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.tool_registry import ToolRegistry
from app.contracts.warning import WarningDeliveryPolicyConfig
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
    ProfileRepository,
    LearnedProfileRepository,
    PsychometricAssessmentRepository,
    WarningScheduleRepository,
)
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.repositories_daily_review import (
    DailyReviewResponseRepository,
    DailyReviewScheduleRepository,
    RetrospectiveCurveRepository,
)
from app.repositories_calendar_mutation import (
    CalendarMutationReconciliationRepository,
)
from app.services.daily_review_service import DailyReviewService
from app.repositories_care import (
    CareInterventionRepository,
    ParticipantCarePreferenceRepository,
)
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.prediction_service import PredictionService
from app.services.pressure_curve_service import PressureCurveService
from app.services.presentation_service import PresentationOutbox
from app.services.card_action_service import CardActionService
from app.services.observation_forecast_refresh import ObservationForecastRefreshService
from app.services.forecast_dependency_refresh import ForecastDependencyRefreshService
from app.services.forecast_mutation_refresh import ForecastMutationRefreshQueue
from app.services.hierarchical_personalization import ParameterLearningService
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
    profile_calibration: ParameterLearningService
    pressure_curves: PressureCurveService
    daily_review_schedules: DailyReviewScheduleRepository
    daily_review_responses: DailyReviewResponseRepository
    retrospective_curves: RetrospectiveCurveRepository
    daily_reviews: DailyReviewService
    observation_refresh: ObservationForecastRefreshService
    dependency_refresh: ForecastDependencyRefreshService
    mutation_refresh: ForecastMutationRefreshQueue
    care_preferences: ParticipantCarePreferenceRepository
    care_interventions: CareInterventionRepository


def build_business_services(
    database: Database, settings: Settings, runs: AgentRunRepository
) -> BusinessServices:
    profiles = ProfileRepository(database)
    observations = ObservationRepository(database)
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
    daily_review_schedules = DailyReviewScheduleRepository(
        database, timezone_name=settings.timezone_name
    )
    daily_review_responses = DailyReviewResponseRepository(database)
    retrospective_curves = RetrospectiveCurveRepository(database)
    daily_reviews = DailyReviewService(
        daily_review_responses, daily_review_schedules, retrospective_curves,
        ForecastSnapshotRepository(database), observations, settings,
    )
    prediction_service = PredictionService(AssessmentModel(settings.timezone_name))
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
    warning_delivery_policy = WarningDeliveryPolicyConfig(
        max_daily_sends=settings.warning_max_daily_sends,
        min_interval_minutes=settings.warning_min_interval_minutes,
    )
    warning_schedules = WarningScheduleRepository(
        database,
        warning_delivery_policy,
        timezone_name=settings.timezone_name,
    )
    daily_reviews.warning_repository = warning_schedules
    care_preferences = ParticipantCarePreferenceRepository(
        database,
        system_max_daily_sends=warning_delivery_policy.max_daily_sends,
        timezone_name=settings.timezone_name,
    )
    care_interventions = CareInterventionRepository(database, care_preferences)
    forecast_snapshots = ForecastSnapshotRepository(database)
    learned_profiles = LearnedProfileRepository(database)
    psychometrics = PsychometricAssessmentRepository(database)
    # Stage 5 replaces per-EMA refitting with a weekly immutable-snapshot run.
    # The scheduler still consumes the small maybe_calibrate interface.
    profile_calibration = ParameterLearningService(
        database,
        settings.timezone_name,
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
        learned_profiles=learned_profiles,
        retrospective_curves=retrospective_curves,
        care_preferences=care_preferences,
    )
    dependency_refresh = ForecastDependencyRefreshService(
        forecast_snapshots,
        warning_schedules,
        forecast_coordinator,
        timezone_name=settings.timezone_name,
    )
    forecast_coordinator.dependency_refresh = dependency_refresh
    daily_reviews.dependency_refresh = dependency_refresh
    mutation_refresh = ForecastMutationRefreshQueue(
        forecast_coordinator,
        reconciliations=CalendarMutationReconciliationRepository(database),
    )
    observation_refresh = ObservationForecastRefreshService(
        forecast_snapshots,
        warning_schedules,
        forecast_coordinator,
        timezone_name=settings.timezone_name,
        dependency_refresh=dependency_refresh,
    )
    card_actions = CardActionService(
        observations,
        calendar,
        timezone_name=settings.timezone_name,
        daily_reviews=daily_reviews,
        observation_refresh=observation_refresh,
        care_interventions=care_interventions,
    )
    pressure_curves = PressureCurveService(
        forecast_coordinator,
        timezone_name=settings.timezone_name,
    )
    registry = ToolRegistry(
        runs, sync_max_concurrency=settings.tool_sync_max_concurrency
    )
    CareTools(
        profiles,
        observations,
        calendar,
        token_repository,
        settings.timezone_name,
        forecast_coordinator,
        forecast_snapshots,
        presentations,
        learned_profiles=learned_profiles,
        pressure_curves=pressure_curves,
        observation_refresh=observation_refresh,
        mutation_refresh=mutation_refresh,
        care_preferences=care_preferences,
        care_interventions=care_interventions,
    ).register(registry)
    return BusinessServices(
        profiles=profiles,
        observations=observations,
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
        observation_refresh=observation_refresh,
        dependency_refresh=dependency_refresh,
        mutation_refresh=mutation_refresh,
        care_preferences=care_preferences,
        care_interventions=care_interventions,
    )
