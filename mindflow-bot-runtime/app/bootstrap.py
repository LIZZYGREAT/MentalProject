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
    ConversationRepository,
    ObservationRepository,
    PredictionRepository,
    ProfileRepository,
)
from app.services.prediction_service import PredictionService
from app.services.token_service import (
    TokenEncryptionService,
    TokenRefreshService,
    TokenRepository,
)
from app.tools.care import CareTools
from mindflow_core.assessment import AssessmentModel


@dataclass(frozen=True)
class BusinessServices:
    profiles: ProfileRepository
    observations: ObservationRepository
    predictions: PredictionRepository
    conversations: ConversationRepository
    token_repository: TokenRepository
    calendar: CalendarService
    prediction_service: PredictionService
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
    registry = ToolRegistry(runs)
    CareTools(
        profiles,
        observations,
        predictions,
        prediction_service,
        calendar,
        token_repository,
        settings.timezone_name,
    ).register(registry)
    return BusinessServices(
        profiles=profiles,
        observations=observations,
        predictions=predictions,
        conversations=conversations,
        token_repository=token_repository,
        calendar=calendar,
        prediction_service=prediction_service,
        registry=registry,
        device_flows=device_flows,
    )
