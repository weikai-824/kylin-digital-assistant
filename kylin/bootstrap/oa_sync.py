"""Composition root for the OA synchronization application."""

from __future__ import annotations

from kylin.adapters.inbound.cli.oa_sync import create_oa_sync_command
from kylin.adapters.outbound.oa.client import OAClient
from kylin.adapters.outbound.oa.config import (
    OA_SETTINGS_PATH,
    load_oa_settings,
    save_oa_settings_template,
)
from kylin.adapters.outbound.oa.file_dispatcher import dispatch as dispatch_oa_attachment
from kylin.adapters.outbound.oa.state import JsonOASyncStateStore
from kylin.applications.knowledge_ingestion.oa_sync import OAKnowledgeSyncService
from kylin.bootstrap.knowledge_ingestion import build_knowledge_ingestion_service


def build_oa_sync_service() -> OAKnowledgeSyncService:
    """Build the OA source, state store, and knowledge ingestion application."""
    settings = load_oa_settings()
    return OAKnowledgeSyncService(
        settings=settings,
        source_factory=lambda: OAClient(settings),
        state_store=JsonOASyncStateStore(),
        ingestion=build_knowledge_ingestion_service(),
        file_dispatcher=dispatch_oa_attachment,
    )


def build_oa_sync_command():
    """在组合根中注入 OA 命令所需的应用服务和配置操作。"""
    return create_oa_sync_command(
        build_oa_sync_service,
        OA_SETTINGS_PATH,
        save_oa_settings_template,
        load_oa_settings,
    )
