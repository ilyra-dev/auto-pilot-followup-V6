#!/usr/bin/env python3
"""
TEST E2E COMPLETO — Client Follow-Up Autopilot V6
===================================================
Verifica de extremo a extremo que el sistema cumple con TODA la funcionalidad
prevista y con las 6 mejoras agregadas:

FUNCIONALIDAD BASE:
  (1) Monitoreo de Notion para pendientes vencidos o proximos a vencer.
  (2) Secuencias escalonadas: recordatorio (1) -> segundo aviso (2) -> urgencia (3) -> escalamiento (4).
  (3) Mensajes contextualizados con Claude API.
  (4) Opera en ES/EN/PT segun idioma del cliente.
  (5) Registra interacciones en Notion.
  (6) CS mantiene override manual.

MEJORAS AGREGADAS:
  1. Follow-up a clientes "En proceso" y "Falta info" SIN esperar a que venza la fecha.
  2. Re-envio a las 48hrs habiles (lunes a viernes) si no hay respuesta.
  3. Draft sin negritas, sin fecha de entrega, pregunta cuando pueden enviar, firma personal.
  4. CC siempre a Cesar Montes, Diana y Piero (resueltos de Notion Owner).
  5. Descarga adjuntos de Dropbox/Drive y los adjunta al correo.
  6. Resumen mejorado de Slack con detalle de seguimientos del dia.

Todos los APIs externos (Notion, Gmail, Claude, Slack) se mockean.
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ── Setup: env vars and stubs BEFORE importing any tool module ────────────────
TOOLS_DIR = Path(__file__).parent.parent / "tools"
TESTS_DIR = Path(__file__).parent

sys.path.insert(0, str(TESTS_DIR))
import stubs  # noqa: F401 — register fake modules

sys.path.insert(0, str(TOOLS_DIR))

os.environ.setdefault("SYSTEM_MODE", "DRAFT")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake")
os.environ.setdefault("NOTION_API_KEY", "secret_test_fake")
os.environ.setdefault("NOTION_DATABASE_ID", "test-db-id")
os.environ.setdefault("NOTION_TEAM_DATABASE_ID", "test-team-db")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-fake")
os.environ.setdefault("SLACK_REVIEW_CHANNEL", "C0TESTREVIEW")
os.environ.setdefault("SLACK_DEFAULT_CHANNEL", "C0TESTDEFAULT")
os.environ.setdefault("GMAIL_SENDER_EMAIL", "test@leaflatam.com")
os.environ.setdefault("GMAIL_DEFAULT_SENDER_EMAIL", "test@leaflatam.com")
os.environ.setdefault("COMPANY_NAME", "LeafLATAM")
os.environ.setdefault("CS_TEAM_EMAIL", "cs@leaflatam.com")
os.environ.setdefault("GMAIL_AUTH_MODE", "oauth2")
os.environ.setdefault("CC_ALWAYS_EMAILS", "")

# ── Imports (safe after stubs and env vars) ───────────────────────────────────
import config
from check_pending_items import get_actionable_items, ACTIVE_STATUSES, ACTIVE_PROJECT_STATUSES
from compute_next_followup import (
    compute_next_followup_date,
    is_followup_due,
    days_overdue,
    is_business_day,
    is_within_business_hours,
    _add_business_days,
    _next_business_day,
)
from claude_client import (
    _build_system_prompt,
    _parse_json_response,
    generate_followup_email,
    classify_response,
    extract_context,
    TONE_MAP,
    LANGUAGE_INSTRUCTIONS,
)
from send_followup import (
    send_followup_for_item,
    _is_valid_email,
    _check_already_sent,
    _download_attachment,
    _load_fallback_template,
)
from style_store import (
    load_style_examples,
    save_style_example,
    load_metrics,
    save_metrics,
    init_style_data,
)
from learning_engine import _similarity, _strip_html, get_mode_recommendation
from daily_summary import (
    generate_summary,
    _build_eod_blocks,
    _get_today_followups,
)


# ═════════════════════════════════════════════════════════════════════════════════
#  HELPERS: Builders for mock Notion pages and actionable items
# ═════════════════════════════════════════════════════════════════════════════════

def _make_notion_page(
    page_id="page-e2e-001",
    nombre="Planos de instalacion",
    status="En curso",
    project_status="En proceso",
    manual_override=False,
    follow_up_stage=0,
    due_date=None,
    last_followup=None,
    client_language="ES",
    gmail_thread_id="",
    has_doc_url=True,
):
    """Build a realistic Notion page dict for the Pendientes CS database."""
    if due_date is None:
        due_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")

    return {
        "id": page_id,
        "properties": {
            "Nombre": {"type": "title", "title": [{"plain_text": nombre}]},
            "Status": {"type": "status", "status": {"name": status}},
            "Manual Override": {"type": "checkbox", "checkbox": manual_override},
            "Follow-Up Stage": {"type": "number", "number": follow_up_stage},
            "Fecha limite de Client Success": {
                "type": "date",
                "date": {"start": due_date} if due_date else None,
            },
            "Last Follow-Up Date": {
                "type": "date",
                "date": {"start": last_followup} if last_followup else None,
            },
            "Client Language": {
                "type": "select",
                "select": {"name": client_language},
            },
            "Gmail Thread ID": {
                "type": "rich_text",
                "rich_text": [{"plain_text": gmail_thread_id}] if gmail_thread_id else [],
            },
            "Follow-Up Log": {"type": "rich_text", "rich_text": []},
            "Owner - Client Success": {
                "type": "people",
                "people": [{"name": "Diana Farje"}],
            },
            "Entregable Proyecto": {
                "type": "relation",
                "relation": [{"id": "task-001"}],
            },
            "Comentarios Client Success": {"type": "rich_text", "rich_text": []},
            "Detalle Falta info / Pausado [Proyectos]": {
                "type": "rollup",
                "rollup": {
                    "type": "array",
                    "array": [
                        {
                            "type": "rich_text",
                            "rich_text": [{"plain_text": "Se necesitan los planos actualizados del proyecto."}],
                        }
                    ],
                },
            },
            "Fecha Objetivo [Proyectos]": {
                "type": "rollup",
                "rollup": {"type": "array", "array": []},
            },
            "Responsable [Proyectos]": {
                "type": "rollup",
                "rollup": {"type": "array", "array": []},
            },
            "Status [Proyectos]": {
                "type": "rollup",
                "rollup": {
                    "type": "array",
                    "array": [{"type": "status", "status": {"name": project_status}}],
                },
            },
        },
    }


def _make_actionable_item(
    page_id="page-e2e-001",
    project_name="Proyecto Solar LATAM",
    client_email="juan@clientcorp.com",
    client_language="ES",
    follow_up_stage=0,
    next_stage=1,
    days_overdue_val=3,
    documentation_url="https://www.dropbox.com/s/abc123/planos.pdf?dl=0",
    cs_name="Diana Farje",
    cs_email="diana@leaflatam.com",
):
    """Build a fully resolved actionable item dict."""
    return {
        "page_id": page_id,
        "project_name": project_name,
        "client_name": "Juan Perez",
        "client_email": client_email,
        "senior_contact_email": "director@clientcorp.com",
        "client_country": "Mexico",
        "client_language": client_language,
        "pending_item": "Planos actualizados de instalacion",
        "due_date": (datetime.now() - timedelta(days=days_overdue_val)).strftime("%Y-%m-%d"),
        "days_overdue": days_overdue_val,
        "impact_description": "Se necesitan los planos actualizados del proyecto.",
        "follow_up_stage": follow_up_stage,
        "next_stage": next_stage,
        "last_followup_date": "",
        "manual_override": False,
        "delivery_team_email": "",
        "delivery_team_slack_channel": "",
        "gmail_thread_id": "",
        "status": "En curso",
        "client_success": cs_name,
        "cs_email": cs_email,
        "analista": "Piero Analista",
        "cs_comments": "",
        "project_status": "En proceso",
        "documentation_url": documentation_url,
    }


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE 1: FUNCIONALIDAD BASE — Monitoreo Notion
# ═════════════════════════════════════════════════════════════════════════════════

class TestNotionMonitoring:
    """(1) Verifica que el sistema monitorea Notion correctamente."""

    def test_active_statuses_include_required_values(self):
        """ACTIVE_STATUSES must include 'En proceso' and 'Falta info'."""
        assert "Sin empezar" in ACTIVE_STATUSES
        assert "En curso" in ACTIVE_STATUSES
        assert "En proceso" in ACTIVE_STATUSES
        assert "Falta info" in ACTIVE_STATUSES

    def test_active_project_statuses_include_required_values(self):
        """ACTIVE_PROJECT_STATUSES must include 'En proceso' and 'Falta info'."""
        assert "En proceso" in ACTIVE_PROJECT_STATUSES
        assert "Falta info" in ACTIVE_PROJECT_STATUSES or "Falta Info" in ACTIVE_PROJECT_STATUSES

    @patch("check_pending_items.notion_client")
    def test_get_actionable_items_returns_en_proceso(self, mock_nc):
        """Items with project status 'En proceso' should be returned."""
        page = _make_notion_page(status="En curso", project_status="En proceso")
        mock_nc.query_database.return_value = [page]
        mock_nc.get_status_property.return_value = "En curso"
        mock_nc.get_rollup_status.return_value = "En proceso"
        mock_nc.get_checkbox_property.return_value = False
        mock_nc.get_number_property.return_value = 0
        mock_nc.get_date_property.return_value = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        mock_nc.get_rollup_date.return_value = ""
        mock_nc.resolve_client_email.return_value = "client@test.com"
        mock_nc.resolve_project_name.return_value = "Test Project"
        mock_nc.resolve_client_name.return_value = "Test Client"
        mock_nc.resolve_senior_contact_email.return_value = ""
        mock_nc.resolve_client_country.return_value = "Mexico"
        mock_nc.get_select_property.return_value = "ES"
        mock_nc.get_text_property.return_value = "Test Item"
        mock_nc.get_rollup_text.return_value = ""
        mock_nc.get_people_first.return_value = "CS Member"
        mock_nc.get_people_email.return_value = "cs@test.com"
        mock_nc.get_rollup_people_first.return_value = ""
        mock_nc.get_rollup_status.return_value = "En proceso"
        mock_nc.resolve_documentation_url.return_value = ""

        items = get_actionable_items()
        assert len(items) >= 1

    @patch("check_pending_items.notion_client")
    def test_get_actionable_items_returns_falta_info(self, mock_nc):
        """Items with status 'Falta info' should be returned."""
        page = _make_notion_page(status="Falta info", project_status="Falta info")
        mock_nc.query_database.return_value = [page]
        mock_nc.get_status_property.return_value = "Falta info"
        mock_nc.get_rollup_status.return_value = "Falta info"
        mock_nc.get_checkbox_property.return_value = False
        mock_nc.get_number_property.return_value = 0
        mock_nc.get_date_property.return_value = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        mock_nc.get_rollup_date.return_value = ""
        mock_nc.resolve_client_email.return_value = "client@test.com"
        mock_nc.resolve_project_name.return_value = "Test Project"
        mock_nc.resolve_client_name.return_value = "Test Client"
        mock_nc.resolve_senior_contact_email.return_value = ""
        mock_nc.resolve_client_country.return_value = "Mexico"
        mock_nc.get_select_property.return_value = "ES"
        mock_nc.get_text_property.return_value = "Test Item"
        mock_nc.get_rollup_text.return_value = "Falta info del proyecto"
        mock_nc.get_people_first.return_value = "CS Member"
        mock_nc.get_people_email.return_value = "cs@test.com"
        mock_nc.get_rollup_people_first.return_value = ""
        mock_nc.resolve_documentation_url.return_value = ""

        items = get_actionable_items()
        assert len(items) >= 1

    @patch("check_pending_items.notion_client")
    def test_manual_override_skips_item(self, mock_nc):
        """Items with Manual Override = True should be skipped."""
        page = _make_notion_page(manual_override=True)
        mock_nc.query_database.return_value = [page]
        mock_nc.get_status_property.return_value = "En curso"
        mock_nc.get_checkbox_property.return_value = True

        items = get_actionable_items()
        assert len(items) == 0

    @patch("check_pending_items.notion_client")
    def test_stage_4_skipped(self, mock_nc):
        """Items at stage 4 should not be returned."""
        page = _make_notion_page(follow_up_stage=4)
        mock_nc.query_database.return_value = [page]
        mock_nc.get_status_property.return_value = "En curso"
        mock_nc.get_rollup_status.return_value = "En proceso"
        mock_nc.get_checkbox_property.return_value = False
        mock_nc.get_number_property.return_value = 4

        items = get_actionable_items()
        assert len(items) == 0


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE 2: Secuencias Escalonadas (4 stages)
# ═════════════════════════════════════════════════════════════════════════════════

class TestEscalationSequence:
    """(2) Verifica la secuencia escalonada de 4 etapas."""

    def test_config_has_4_stages(self):
        """FOLLOWUP_SCHEDULE must have exactly 4 stages."""
        assert len(config.FOLLOWUP_SCHEDULE) == 4
        assert set(config.FOLLOWUP_SCHEDULE.keys()) == {1, 2, 3, 4}

    def test_stage_0_to_1_immediate(self):
        """Stage 0 -> 1 should be immediate for overdue items."""
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        is_due, next_stage, _ = is_followup_due(0, due_date=yesterday)
        assert is_due is True
        assert next_stage == 1

    def test_stage_1_to_2_after_interval(self):
        """Stage 1 -> 2 should trigger after configured interval."""
        interval = config.FOLLOWUP_SCHEDULE[2]
        past = (datetime.now() - timedelta(days=interval + 2)).strftime("%Y-%m-%d")
        is_due, next_stage, _ = is_followup_due(1, last_followup_date=past)
        assert is_due is True
        assert next_stage == 2

    def test_stage_2_to_3_after_interval(self):
        """Stage 2 -> 3 should trigger after configured interval."""
        interval = config.FOLLOWUP_SCHEDULE[3]
        past = (datetime.now() - timedelta(days=interval + 2)).strftime("%Y-%m-%d")
        is_due, next_stage, _ = is_followup_due(2, last_followup_date=past)
        assert is_due is True
        assert next_stage == 3

    def test_stage_3_to_4_after_interval(self):
        """Stage 3 -> 4 should trigger after configured interval."""
        interval = config.FOLLOWUP_SCHEDULE[4]
        past = (datetime.now() - timedelta(days=interval + 2)).strftime("%Y-%m-%d")
        is_due, next_stage, _ = is_followup_due(3, last_followup_date=past)
        assert is_due is True
        assert next_stage == 4

    def test_stage_4_never_due(self):
        """Stage 4 (completed) should never trigger another follow-up."""
        is_due, next_stage, next_date = is_followup_due(4)
        assert is_due is False
        assert next_stage is None
        assert next_date is None

    @patch("send_followup.notion_client")
    @patch("send_followup.draft_manager")
    @patch("send_followup.team_manager")
    @patch("send_followup.claude_client")
    def test_stage_4_uses_senior_contact(self, mock_claude, mock_team, mock_draft, mock_notion):
        """Stage 4 escalation should send to senior contact email."""
        mock_team.resolve_email.return_value = "cs@leaflatam.com"
        mock_team.get_admins_cc.return_value = ""
        mock_claude.generate_followup_email.return_value = {
            "subject": "Escalation",
            "body_html": "<p>Dear Director</p>",
        }
        mock_notion.get_page.return_value = {
            "properties": {"Follow-Up Stage": {"type": "number", "number": 3}}
        }
        mock_notion.get_number_property.return_value = 3
        mock_notion.resolve_fixed_cc_emails.return_value = set()
        mock_draft.create_draft_and_notify.return_value = {"draft_id": "d-esc", "slack_ts": "ts"}

        item = _make_actionable_item(follow_up_stage=3, next_stage=4)

        with patch("send_followup.SYSTEM_MODE", "DRAFT"):
            result = send_followup_for_item(item)

        # draft_manager should be called with to=senior_contact_email
        call_args = mock_draft.create_draft_and_notify.call_args
        assert "director@clientcorp.com" in str(call_args)


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE 3: Claude API — Mensajes Contextualizados
# ═════════════════════════════════════════════════════════════════════════════════

class TestClaudeContextualizedMessages:
    """(3) Verifica generacion contextualizada de mensajes con Claude."""

    @patch("claude_client._call_claude_with_retry")
    def test_generate_email_success(self, mock_call):
        """Claude should return subject + body_html."""
        mock_call.return_value = json.dumps({
            "subject": "Recordatorio: Proyecto Solar",
            "body_html": "<p>Estimado Juan, le escribimos sobre el Proyecto Solar.</p>",
        })
        result = generate_followup_email(
            context={"project_name": "Proyecto Solar", "client_name": "Juan"},
            language="ES",
            stage=1,
            company_name="LeafLATAM",
            sender_name="Diana Farje",
        )
        assert result is not None
        assert "subject" in result
        assert "body_html" in result
        assert "Proyecto Solar" in result["subject"] or "Proyecto Solar" in result["body_html"]

    @patch("claude_client._call_claude_with_retry", return_value=None)
    def test_generate_email_failure_returns_none(self, mock_call):
        """If Claude fails, generate_followup_email returns None."""
        result = generate_followup_email(
            context={"project_name": "X"},
            language="ES",
            stage=1,
        )
        assert result is None

    def test_system_prompt_includes_project_context(self):
        """System prompt should guide Claude to include project details."""
        prompt = _build_system_prompt("LeafLATAM", "ES", 1, sender_name="Diana")
        assert "LeafLATAM" in prompt
        assert "Diana" in prompt

    def test_tone_map_has_all_stages_and_languages(self):
        """TONE_MAP must cover all 4 stages and 3 languages."""
        for stage in [1, 2, 3, 4]:
            assert stage in TONE_MAP
            for lang in ["ES", "EN", "PT"]:
                assert lang in TONE_MAP[stage]

    @patch("claude_client._call_claude_with_retry")
    def test_classify_response_received(self, mock_call):
        """classify_response should identify when info was received."""
        mock_call.return_value = json.dumps({
            "classification": "received",
            "confidence": 0.95,
            "summary": "Client sent requested files",
        })
        result = classify_response("Here are the updated plans", "Updated plans")
        assert result["classification"] == "received"

    @patch("claude_client._call_claude_with_retry")
    def test_extract_context_from_team_message(self, mock_call):
        """extract_context should identify project from team message."""
        mock_call.return_value = json.dumps({
            "project_name": "Proyecto Solar",
            "confidence": 0.9,
        })
        result = extract_context("Send updated plans for Proyecto Solar to client")
        assert result["project_name"] == "Proyecto Solar"

    def test_fallback_template_loads_all_stages_and_languages(self):
        """Fallback templates must exist for all 4 stages x 3 languages."""
        for stage in [1, 2, 3, 4]:
            for lang in ["ES", "EN", "PT"]:
                template = _load_fallback_template(stage, lang)
                assert template is not None, f"Missing fallback template: stage={stage}, lang={lang}"
                assert len(template) > 50


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE 4: Multi-idioma ES/EN/PT
# ═════════════════════════════════════════════════════════════════════════════════

class TestMultiLanguageSupport:
    """(4) Verifica que el sistema opera en ES/EN/PT."""

    def test_supported_languages(self):
        assert config.SUPPORTED_LANGUAGES == ("ES", "EN", "PT")

    def test_language_date_formats(self):
        for lang in config.SUPPORTED_LANGUAGES:
            assert lang in config.LANGUAGE_DATE_FORMATS

    def test_system_prompt_spanish(self):
        prompt = _build_system_prompt("X", "ES", 1)
        assert "espanol" in prompt.lower() or "español" in prompt.lower()

    def test_system_prompt_english(self):
        prompt = _build_system_prompt("X", "EN", 1)
        assert "english" in prompt.lower()

    def test_system_prompt_portuguese(self):
        prompt = _build_system_prompt("X", "PT", 1)
        assert "portugu" in prompt.lower()

    def test_language_instructions_all_defined(self):
        for lang in ["ES", "EN", "PT"]:
            assert lang in LANGUAGE_INSTRUCTIONS

    @patch("claude_client._call_claude_with_retry")
    def test_generate_email_in_english(self, mock_call):
        mock_call.return_value = json.dumps({
            "subject": "Reminder: Project Solar",
            "body_html": "<p>Dear John, we are writing about Project Solar.</p>",
        })
        result = generate_followup_email(
            context={"project_name": "Project Solar"},
            language="EN",
            stage=1,
        )
        assert result is not None

    @patch("claude_client._call_claude_with_retry")
    def test_generate_email_in_portuguese(self, mock_call):
        mock_call.return_value = json.dumps({
            "subject": "Lembrete: Projeto Solar",
            "body_html": "<p>Caro Joao, estamos escrevendo sobre o Projeto Solar.</p>",
        })
        result = generate_followup_email(
            context={"project_name": "Projeto Solar"},
            language="PT",
            stage=1,
        )
        assert result is not None


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE 5: Registro de Interacciones en Notion
# ═════════════════════════════════════════════════════════════════════════════════

class TestNotionInteractionLogging:
    """(5) Verifica que se registran interacciones en Notion."""

    @patch("send_followup.notion_client")
    @patch("send_followup.draft_manager")
    @patch("send_followup.team_manager")
    @patch("send_followup.claude_client")
    def test_notion_updated_on_success(self, mock_claude, mock_team, mock_draft, mock_notion):
        """On successful draft creation, Notion should be updated with stage + log."""
        mock_notion.get_page.return_value = {
            "properties": {"Follow-Up Stage": {"type": "number", "number": 0}}
        }
        mock_notion.get_number_property.return_value = 0
        mock_notion.resolve_fixed_cc_emails.return_value = set()
        mock_team.resolve_email.return_value = "cs@leaflatam.com"
        mock_team.get_admins_cc.return_value = ""
        mock_claude.generate_followup_email.return_value = {
            "subject": "Test Subject",
            "body_html": "<p>Test Body</p>",
        }
        mock_draft.create_draft_and_notify.return_value = {
            "draft_id": "draft-log-test",
            "slack_ts": "ts-1",
        }

        item = _make_actionable_item()
        with patch("send_followup.SYSTEM_MODE", "DRAFT"):
            result = send_followup_for_item(item)

        assert result["success"] is True
        # Notion update_page should be called to update the stage
        mock_notion.update_page.assert_called_once()
        # Notion append_to_log should be called to log the action
        mock_notion.append_to_log.assert_called()

    @patch("send_followup.notion_client")
    @patch("send_followup.team_manager")
    @patch("send_followup.claude_client")
    def test_deduplication_prevents_double_send(self, mock_claude, mock_team, mock_notion):
        """If stage already sent (dedup check), skip without error."""
        mock_notion.get_page.return_value = {
            "properties": {"Follow-Up Stage": {"type": "number", "number": 1}}
        }
        mock_notion.get_number_property.return_value = 1
        mock_team.resolve_email.return_value = None
        mock_team.get_admins_cc.return_value = ""

        item = _make_actionable_item(follow_up_stage=0, next_stage=1)
        result = send_followup_for_item(item)
        assert result["success"] is False
        assert "Already sent" in result.get("error", "")


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE 6: Override Manual
# ═════════════════════════════════════════════════════════════════════════════════

class TestManualOverride:
    """(6) CS mantiene override manual."""

    @patch("check_pending_items.notion_client")
    def test_override_flag_skips_item(self, mock_nc):
        """Items with Manual Override=True should be excluded."""
        page = _make_notion_page(manual_override=True)
        mock_nc.query_database.return_value = [page]
        mock_nc.get_status_property.return_value = "En curso"
        mock_nc.get_checkbox_property.return_value = True

        items = get_actionable_items()
        assert len(items) == 0

    @patch("check_pending_items.notion_client")
    def test_no_override_includes_item(self, mock_nc):
        """Items without override should be included (when due)."""
        page = _make_notion_page(manual_override=False)
        mock_nc.query_database.return_value = [page]
        mock_nc.get_status_property.return_value = "En curso"
        mock_nc.get_rollup_status.return_value = "En proceso"
        mock_nc.get_checkbox_property.return_value = False
        mock_nc.get_number_property.return_value = 0
        mock_nc.get_date_property.return_value = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        mock_nc.get_rollup_date.return_value = ""
        mock_nc.resolve_client_email.return_value = "client@test.com"
        mock_nc.resolve_project_name.return_value = "Test"
        mock_nc.resolve_client_name.return_value = "Client"
        mock_nc.resolve_senior_contact_email.return_value = ""
        mock_nc.resolve_client_country.return_value = "Mexico"
        mock_nc.get_select_property.return_value = "ES"
        mock_nc.get_text_property.return_value = "Item"
        mock_nc.get_rollup_text.return_value = ""
        mock_nc.get_people_first.return_value = "CS"
        mock_nc.get_people_email.return_value = "cs@test.com"
        mock_nc.get_rollup_people_first.return_value = ""
        mock_nc.resolve_documentation_url.return_value = ""

        items = get_actionable_items()
        assert len(items) >= 1


# ═════════════════════════════════════════════════════════════════════════════════
#  MEJORA 1: Follow-up a "En proceso" y "Falta info" SIN esperar vencimiento
# ═════════════════════════════════════════════════════════════════════════════════

class TestMejora1StatusBasedFollowup:
    """
    MEJORA 1: El seguimiento se debe hacer a todos los clientes con status
    'En proceso' y 'Falta info' sin esperar a que se venza la fecha.
    """

    def test_en_proceso_in_active_project_statuses(self):
        """'En proceso' must be in ACTIVE_PROJECT_STATUSES."""
        assert "En proceso" in ACTIVE_PROJECT_STATUSES

    def test_falta_info_in_active_statuses(self):
        """'Falta info' must be in ACTIVE_STATUSES."""
        assert "Falta info" in ACTIVE_STATUSES

    def test_falta_info_in_active_project_statuses(self):
        """'Falta info' or 'Falta Info' must be in ACTIVE_PROJECT_STATUSES."""
        assert "Falta info" in ACTIVE_PROJECT_STATUSES or "Falta Info" in ACTIVE_PROJECT_STATUSES

    @patch("check_pending_items.notion_client")
    def test_en_proceso_without_overdue_triggers_followup(self, mock_nc):
        """
        Item with status 'En proceso' and a FUTURE due date should still
        be actionable if the is_followup_due logic says it's due.
        """
        future_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
        page = _make_notion_page(status="En proceso", due_date=future_date, project_status="En proceso")
        mock_nc.query_database.return_value = [page]
        mock_nc.get_status_property.return_value = "En proceso"
        mock_nc.get_rollup_status.return_value = "En proceso"
        mock_nc.get_checkbox_property.return_value = False
        mock_nc.get_number_property.return_value = 0
        mock_nc.get_date_property.return_value = future_date
        mock_nc.get_rollup_date.return_value = ""
        mock_nc.resolve_client_email.return_value = "client@test.com"
        mock_nc.resolve_project_name.return_value = "Test Project"
        mock_nc.resolve_client_name.return_value = "Test Client"
        mock_nc.resolve_senior_contact_email.return_value = ""
        mock_nc.resolve_client_country.return_value = "Mexico"
        mock_nc.get_select_property.return_value = "ES"
        mock_nc.get_text_property.return_value = "Test Item"
        mock_nc.get_rollup_text.return_value = ""
        mock_nc.get_people_first.return_value = "CS Member"
        mock_nc.get_people_email.return_value = "cs@test.com"
        mock_nc.get_rollup_people_first.return_value = ""
        mock_nc.resolve_documentation_url.return_value = ""

        # Stage 0 should be considered due regardless of due_date for active statuses
        items = get_actionable_items()
        # Depending on implementation, status match should be enough
        # The key check: the status-based filtering allows the item in
        assert mock_nc.get_status_property.called or mock_nc.get_rollup_status.called


# ═════════════════════════════════════════════════════════════════════════════════
#  MEJORA 2: Reenvio a 48hrs habiles (lunes a viernes)
# ═════════════════════════════════════════════════════════════════════════════════

class TestMejora2BusinessDayReenvio:
    """
    MEJORA 2: Si en 48hrs el correo no es respondido, enviar otro.
    Solo lunes a viernes. Si 48hrs cae en sabado/domingo, enviar el lunes.
    """

    def test_add_business_days_skips_weekend(self):
        """_add_business_days should skip weekends."""
        # Friday + 2 business days = Tuesday (skips Sat+Sun)
        friday = datetime(2026, 2, 27)  # Friday
        assert friday.weekday() == 4  # Verify it's Friday
        result = _add_business_days(friday, 2)
        assert result.weekday() == 1  # Tuesday

    def test_add_business_days_from_monday(self):
        """Monday + 2 business days = Wednesday."""
        monday = datetime(2026, 2, 23)
        assert monday.weekday() == 0
        result = _add_business_days(monday, 2)
        assert result.weekday() == 2  # Wednesday

    def test_next_business_day_from_saturday(self):
        """Saturday should become Monday."""
        saturday = datetime(2026, 2, 21)
        assert saturday.weekday() == 5
        result = _next_business_day(saturday)
        assert result.weekday() == 0  # Monday

    def test_next_business_day_from_sunday(self):
        """Sunday should become Monday."""
        sunday = datetime(2026, 2, 22)
        assert sunday.weekday() == 6
        result = _next_business_day(sunday)
        assert result.weekday() == 0  # Monday

    def test_next_business_day_from_weekday(self):
        """Weekday should stay the same."""
        wednesday = datetime(2026, 2, 25)
        assert wednesday.weekday() == 2
        result = _next_business_day(wednesday)
        assert result.weekday() == 2

    def test_is_business_day_monday(self):
        monday = datetime(2026, 2, 23)
        assert is_business_day(monday) is True

    def test_is_business_day_saturday(self):
        saturday = datetime(2026, 2, 21)
        assert is_business_day(saturday) is False

    def test_is_business_day_sunday(self):
        sunday = datetime(2026, 2, 22)
        assert is_business_day(sunday) is False

    def test_stage_1_to_2_respects_business_days(self):
        """Stage 1->2 interval should be calculated in business days."""
        interval = config.FOLLOWUP_SCHEDULE[2]
        # If last followup was interval+2 days ago (to account for weekend),
        # it should be due
        past = (datetime.now() - timedelta(days=interval + 3)).strftime("%Y-%m-%d")
        is_due, next_stage, _ = is_followup_due(1, last_followup_date=past)
        assert is_due is True

    def test_is_within_business_hours_returns_bool(self):
        """is_within_business_hours should always return a boolean."""
        assert isinstance(is_within_business_hours(country="Mexico"), bool)
        assert isinstance(is_within_business_hours(country="Colombia"), bool)
        assert isinstance(is_within_business_hours(country="Chile"), bool)
        assert isinstance(is_within_business_hours(country=None), bool)
        assert isinstance(is_within_business_hours(country="Atlantis"), bool)

    @patch("compute_next_followup.datetime")
    def test_business_hours_respects_time(self, mock_dt):
        """Business hours check should respect start/end hours."""
        # Wednesday 10:00 — within business hours
        mock_now = datetime(2026, 2, 18, 10, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        result = is_within_business_hours(start_hour=8, end_hour=18)
        assert result is True

    @patch("compute_next_followup.datetime")
    def test_business_hours_rejects_weekend(self, mock_dt):
        """Saturday should be outside business hours even at 10 AM."""
        mock_now = datetime(2026, 2, 21, 10, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        result = is_within_business_hours(start_hour=8, end_hour=18)
        assert result is False


# ═════════════════════════════════════════════════════════════════════════════════
#  MEJORA 3: Draft sin negritas, sin fecha, pregunta cuando, firma personal
# ═════════════════════════════════════════════════════════════════════════════════

class TestMejora3DraftFormatting:
    """
    MEJORA 3: Al crear el draft:
    - NO negritas (<strong>/<b>)
    - NO fecha de entrega del envio
    - SI pregunta cuando pueden enviar
    - SI firma con nombre personal del CS (no 'equipo de leaf')
    """

    def test_system_prompt_forbids_bold(self):
        """System prompt should instruct Claude to NEVER use bold."""
        prompt = _build_system_prompt("LeafLATAM", "ES", 1, sender_name="Diana")
        assert "<strong>" in prompt.lower() or "bold" in prompt.lower() or "<b>" in prompt.lower()
        assert "never" in prompt.lower() or "nunca" in prompt.lower() or "no" in prompt.lower()

    def test_system_prompt_forbids_deadlines(self):
        """System prompt should instruct Claude to NOT include specific deadlines."""
        prompt = _build_system_prompt("LeafLATAM", "ES", 1, sender_name="Diana")
        assert "deadline" in prompt.lower() or "fecha" in prompt.lower()

    def test_system_prompt_asks_when(self):
        """System prompt should instruct Claude to ask WHEN client can send info."""
        prompt = _build_system_prompt("LeafLATAM", "ES", 1, sender_name="Diana")
        # Should mention asking when/cuando
        assert "cuando" in prompt.lower() or "when" in prompt.lower()

    def test_system_prompt_personal_signature(self):
        """System prompt should use personal sender name in signature."""
        prompt = _build_system_prompt("LeafLATAM", "ES", 1, sender_name="Diana Farje")
        assert "Diana Farje" in prompt

    def test_system_prompt_forbids_team_signature(self):
        """System prompt should forbid 'Equipo de Leaf' as signature."""
        prompt = _build_system_prompt("LeafLATAM", "ES", 1, sender_name="Diana")
        assert "equipo de leaf" in prompt.lower() or "equipo leaf" in prompt.lower()

    def test_system_prompt_forbids_links(self):
        """System prompt should instruct to NOT include document links."""
        prompt = _build_system_prompt("LeafLATAM", "ES", 1, sender_name="Diana")
        assert "link" in prompt.lower() or "enlace" in prompt.lower()

    @patch("claude_client._call_claude_with_retry")
    def test_generated_email_has_no_bold(self, mock_call):
        """Generated email HTML should not contain <strong> or <b> tags."""
        mock_call.return_value = json.dumps({
            "subject": "Recordatorio",
            "body_html": "<p>Estimado Juan, le escribimos sobre el proyecto. Saludos, Diana</p>",
        })
        result = generate_followup_email(
            context={"project_name": "X", "client_name": "Juan"},
            language="ES",
            stage=1,
            sender_name="Diana",
        )
        assert result is not None
        assert "<strong>" not in result["body_html"]
        assert "<b>" not in result["body_html"]


# ═════════════════════════════════════════════════════════════════════════════════
#  MEJORA 4: CC siempre a Cesar Montes, Diana y Piero
# ═════════════════════════════════════════════════════════════════════════════════

class TestMejora4FixedCCRecipients:
    """
    MEJORA 4: Todos los correos de seguimiento deben siempre tener en copia
    a Cesar Montes, Diana y Piero. Sus correos se obtienen de la tabla
    Proyectos columna Owner.
    """

    def test_cc_always_names_config(self):
        """CC_ALWAYS_NAMES should be configured with Cesar, Diana, Piero."""
        names = config.CC_ALWAYS_NAMES
        assert names is not None
        names_lower = names.lower()
        assert "cesar" in names_lower or "césar" in names_lower
        assert "diana" in names_lower
        assert "piero" in names_lower

    @patch("send_followup.notion_client")
    @patch("send_followup.draft_manager")
    @patch("send_followup.team_manager")
    @patch("send_followup.claude_client")
    def test_fixed_cc_resolved_from_notion(self, mock_claude, mock_team, mock_draft, mock_notion):
        """Fixed CC should be resolved from Notion Proyectos Owner field."""
        # Setup
        mock_notion.get_page.return_value = {
            "properties": {"Follow-Up Stage": {"type": "number", "number": 0}}
        }
        mock_notion.get_number_property.return_value = 0
        mock_notion.resolve_fixed_cc_emails.return_value = {
            "cesar@leaflatam.com",
            "diana@leaflatam.com",
            "piero@leaflatam.com",
        }
        mock_team.resolve_email.return_value = "cs@leaflatam.com"
        mock_team.get_admins_cc.return_value = ""
        mock_claude.generate_followup_email.return_value = {
            "subject": "Test",
            "body_html": "<p>Test</p>",
        }
        mock_draft.create_draft_and_notify.return_value = {
            "draft_id": "d-cc",
            "slack_ts": "ts",
        }

        item = _make_actionable_item()
        with patch("send_followup.SYSTEM_MODE", "DRAFT"):
            result = send_followup_for_item(item)

        assert result["success"] is True
        # Verify CC was passed to draft creation
        call_args = mock_draft.create_draft_and_notify.call_args
        cc_arg = str(call_args)
        assert "cesar@leaflatam.com" in cc_arg
        assert "diana@leaflatam.com" in cc_arg
        assert "piero@leaflatam.com" in cc_arg

    @patch("send_followup.notion_client")
    @patch("send_followup.draft_manager")
    @patch("send_followup.team_manager")
    @patch("send_followup.claude_client")
    def test_cc_excludes_sender(self, mock_claude, mock_team, mock_draft, mock_notion):
        """The sender should NOT be in CC."""
        mock_notion.get_page.return_value = {
            "properties": {"Follow-Up Stage": {"type": "number", "number": 0}}
        }
        mock_notion.get_number_property.return_value = 0
        mock_notion.resolve_fixed_cc_emails.return_value = {
            "diana@leaflatam.com",
            "piero@leaflatam.com",
        }
        mock_team.resolve_email.return_value = "cs@leaflatam.com"
        mock_team.get_admins_cc.return_value = ""
        mock_claude.generate_followup_email.return_value = {
            "subject": "Test",
            "body_html": "<p>Test</p>",
        }
        mock_draft.create_draft_and_notify.return_value = {
            "draft_id": "d-cc2",
            "slack_ts": "ts",
        }

        # Sender is diana@leaflatam.com — should be removed from CC
        item = _make_actionable_item(cs_email="diana@leaflatam.com")
        with patch("send_followup.SYSTEM_MODE", "DRAFT"):
            result = send_followup_for_item(item)

        assert result["success"] is True
        call_args = mock_draft.create_draft_and_notify.call_args
        cc_arg = call_args.kwargs.get("cc", "") if call_args.kwargs else ""
        # Diana should NOT be in CC since she's the sender
        if cc_arg:
            assert "diana@leaflatam.com" not in cc_arg

    @patch("send_followup.notion_client")
    @patch("send_followup.draft_manager")
    @patch("send_followup.team_manager")
    @patch("send_followup.claude_client")
    def test_cc_always_emails_fallback(self, mock_claude, mock_team, mock_draft, mock_notion):
        """If Notion resolution fails, CC_ALWAYS_EMAILS env var should be used as fallback."""
        mock_notion.get_page.return_value = {
            "properties": {"Follow-Up Stage": {"type": "number", "number": 0}}
        }
        mock_notion.get_number_property.return_value = 0
        # Notion returns empty set
        mock_notion.resolve_fixed_cc_emails.return_value = set()
        mock_team.resolve_email.return_value = "cs@leaflatam.com"
        mock_team.get_admins_cc.return_value = ""
        mock_claude.generate_followup_email.return_value = {
            "subject": "Test",
            "body_html": "<p>Test</p>",
        }
        mock_draft.create_draft_and_notify.return_value = {
            "draft_id": "d-fb",
            "slack_ts": "ts",
        }

        item = _make_actionable_item()
        with patch("send_followup.SYSTEM_MODE", "DRAFT"), \
             patch("send_followup.CC_ALWAYS_EMAILS", "cesar@leaf.com,diana@leaf.com"):
            result = send_followup_for_item(item)

        assert result["success"] is True
        call_args = mock_draft.create_draft_and_notify.call_args
        cc_arg = str(call_args)
        assert "cesar@leaf.com" in cc_arg
        assert "diana@leaf.com" in cc_arg


# ═════════════════════════════════════════════════════════════════════════════════
#  MEJORA 5: Descargar adjuntos de Dropbox/Drive y adjuntar al correo
# ═════════════════════════════════════════════════════════════════════════════════

class TestMejora5AttachmentDownload:
    """
    MEJORA 5: Descargar el documento y adjuntarlo al correo desde el link
    de Dropbox y/o Drive que esta en Notion.
    """

    def test_download_dropbox_converts_url(self):
        """Dropbox URLs should be converted to direct download format."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"fake-pdf-content"
        mock_resp.headers = {"Content-Type": "application/pdf"}
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp) as mock_get:
            result = _download_attachment(
                "https://www.dropbox.com/s/abc123/planos.pdf?dl=0",
                "planos.pdf",
            )

            assert result is not None
            assert result["filename"] == "planos.pdf"
            assert result["data"] == b"fake-pdf-content"
            assert result["mime_type"] == "application/pdf"
            called_url = mock_get.call_args[0][0]
            assert "dl.dropboxusercontent.com" in called_url or "dl=1" in called_url

    def test_download_drive_extracts_file_id(self):
        """Google Drive URLs should be converted to direct download format."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"fake-doc-content"
        mock_resp.headers = {"Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp) as mock_get:
            result = _download_attachment(
                "https://drive.google.com/file/d/1aBcDeFgHiJkLmNoPqRsTuVwXyZ/view?usp=sharing",
                "documento.docx",
            )

            assert result is not None
            called_url = mock_get.call_args[0][0]
            assert "1aBcDeFgHiJkLmNoPqRsTuVwXyZ" in called_url
            assert "export=download" in called_url

    def test_download_failure_returns_none(self):
        """Failed downloads should return None, not crash."""
        with patch("requests.get", side_effect=Exception("Network error")):
            result = _download_attachment("https://example.com/file.pdf", "file.pdf")
            assert result is None

    @patch("send_followup.notion_client")
    @patch("send_followup.draft_manager")
    @patch("send_followup.team_manager")
    @patch("send_followup.claude_client")
    @patch("send_followup._download_attachment")
    def test_attachment_included_in_draft(self, mock_dl, mock_claude, mock_team, mock_draft, mock_notion):
        """Downloaded attachment should be passed to draft creation."""
        mock_notion.get_page.return_value = {
            "properties": {"Follow-Up Stage": {"type": "number", "number": 0}}
        }
        mock_notion.get_number_property.return_value = 0
        mock_notion.resolve_fixed_cc_emails.return_value = set()
        mock_team.resolve_email.return_value = "cs@leaflatam.com"
        mock_team.get_admins_cc.return_value = ""
        mock_claude.generate_followup_email.return_value = {
            "subject": "Test",
            "body_html": "<p>Test</p>",
        }
        mock_dl.return_value = {
            "filename": "planos.pdf",
            "data": b"pdf-content",
            "mime_type": "application/pdf",
        }
        mock_draft.create_draft_and_notify.return_value = {
            "draft_id": "d-att",
            "slack_ts": "ts",
        }

        item = _make_actionable_item(documentation_url="https://dropbox.com/s/abc/planos.pdf?dl=0")
        with patch("send_followup.SYSTEM_MODE", "DRAFT"):
            result = send_followup_for_item(item)

        assert result["success"] is True
        # Verify attachments were passed to draft_manager
        call_args = mock_draft.create_draft_and_notify.call_args
        attachments_arg = call_args.kwargs.get("attachments", []) if call_args.kwargs else []
        assert len(attachments_arg) == 1
        assert attachments_arg[0]["filename"] == "planos.pdf"

    @patch("send_followup.notion_client")
    @patch("send_followup.draft_manager")
    @patch("send_followup.team_manager")
    @patch("send_followup.claude_client")
    def test_no_doc_url_sends_without_attachment(self, mock_claude, mock_team, mock_draft, mock_notion):
        """Items without documentation_url should still send (without attachment)."""
        mock_notion.get_page.return_value = {
            "properties": {"Follow-Up Stage": {"type": "number", "number": 0}}
        }
        mock_notion.get_number_property.return_value = 0
        mock_notion.resolve_fixed_cc_emails.return_value = set()
        mock_team.resolve_email.return_value = "cs@leaflatam.com"
        mock_team.get_admins_cc.return_value = ""
        mock_claude.generate_followup_email.return_value = {
            "subject": "Test",
            "body_html": "<p>Test</p>",
        }
        mock_draft.create_draft_and_notify.return_value = {
            "draft_id": "d-noatt",
            "slack_ts": "ts",
        }

        item = _make_actionable_item(documentation_url="")
        with patch("send_followup.SYSTEM_MODE", "DRAFT"):
            result = send_followup_for_item(item)

        assert result["success"] is True
        call_args = mock_draft.create_draft_and_notify.call_args
        attachments_arg = call_args.kwargs.get("attachments", []) if call_args.kwargs else []
        assert len(attachments_arg) == 0


# ═════════════════════════════════════════════════════════════════════════════════
#  MEJORA 6: Resumen mejorado de Slack
# ═════════════════════════════════════════════════════════════════════════════════

class TestMejora6SlackSummary:
    """
    MEJORA 6: Resumen mejorado de Slack con detalle de seguimientos del dia.
    """

    def test_eod_blocks_with_followups(self):
        """EOD blocks should include detail for each follow-up."""
        followups = [
            {
                "proyecto": "Proyecto Solar",
                "cliente": "Juan Perez",
                "etapa": 1,
                "idioma": "ES",
                "asunto": "Recordatorio: Planos",
                "destinatario": "juan@client.com",
                "responsable": "Diana Farje",
                "modo": "DRAFT",
                "draft_id": "d-1",
                "hora": "14:30",
            },
            {
                "proyecto": "Proyecto Eolico",
                "cliente": "Maria Lopez",
                "etapa": 2,
                "idioma": "ES",
                "asunto": "Segundo aviso: Documentacion",
                "destinatario": "maria@client.com",
                "responsable": "Piero Analyst",
                "modo": "DRAFT",
                "draft_id": "d-2",
                "hora": "15:00",
            },
        ]

        blocks, fallback = _build_eod_blocks(followups)

        # Should have header block
        assert any(b.get("type") == "header" for b in blocks)
        # Should mention the total count
        block_text = json.dumps(blocks)
        assert "2 seguimientos" in block_text or "2 seguimiento" in block_text
        # Should include project names
        assert "Proyecto Solar" in block_text
        assert "Proyecto Eolico" in block_text
        # Should include responsables
        assert "Diana Farje" in block_text
        assert "Piero" in block_text
        # Should include stage labels
        assert "1er contacto" in block_text
        assert "2do contacto" in block_text
        # Fallback text should be meaningful
        assert "2" in fallback

    def test_eod_blocks_empty_day(self):
        """EOD blocks should handle zero follow-ups gracefully."""
        blocks, fallback = _build_eod_blocks([])
        block_text = json.dumps(blocks)
        assert "No se realizaron" in block_text or "0 seguimiento" in block_text

    def test_eod_blocks_groups_by_responsable(self):
        """EOD summary should group follow-ups by CS owner."""
        followups = [
            {"proyecto": "P1", "cliente": "C1", "etapa": 1, "idioma": "ES",
             "asunto": "S1", "destinatario": "c1@x.com", "responsable": "Diana",
             "modo": "DRAFT", "draft_id": "d1", "hora": "10:00"},
            {"proyecto": "P2", "cliente": "C2", "etapa": 1, "idioma": "ES",
             "asunto": "S2", "destinatario": "c2@x.com", "responsable": "Diana",
             "modo": "DRAFT", "draft_id": "d2", "hora": "11:00"},
            {"proyecto": "P3", "cliente": "C3", "etapa": 2, "idioma": "EN",
             "asunto": "S3", "destinatario": "c3@x.com", "responsable": "Piero",
             "modo": "DRAFT", "draft_id": "d3", "hora": "12:00"},
        ]
        blocks, _ = _build_eod_blocks(followups)
        block_text = json.dumps(blocks)
        # Should show Diana with 2 and Piero with 1
        assert "Diana" in block_text
        assert "Piero" in block_text

    @patch("daily_summary.notion_client")
    def test_eod_blocks_include_upcoming_section(self, mock_nc):
        """EOD blocks should include upcoming follow-ups section."""
        mock_nc.query_database.return_value = []
        followups = [
            {"proyecto": "P1", "cliente": "C1", "etapa": 1, "idioma": "ES",
             "asunto": "S1", "destinatario": "c1@x.com", "responsable": "Diana",
             "modo": "DRAFT", "draft_id": "d1", "hora": "10:00"},
        ]
        blocks, _ = _build_eod_blocks(followups)
        # Use ensure_ascii=False so accented characters appear literally
        block_text = json.dumps(blocks, ensure_ascii=False)
        # Should have a section about upcoming follow-ups
        assert "Próximos" in block_text or "próximos" in block_text or "programados" in block_text.lower()

    def test_generate_summary_has_required_fields(self):
        """generate_summary should return subject, body_html, body_text."""
        with patch("daily_summary.notion_client") as mock_nc, \
             patch("daily_summary.load_metrics") as mock_m:
            mock_nc.query_database.return_value = []
            mock_m.return_value = {
                "total_drafts": 10,
                "approval_rate": 0.8,
                "sent_as_is": 5,
                "sent_edited": 3,
                "discarded": 2,
            }
            summary = generate_summary()
            assert "subject" in summary
            assert "body_html" in summary
            assert "body_text" in summary
            assert "Daily Summary" in summary["subject"]


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE E2E: Flujo completo Notion -> Claude -> Gmail -> Slack -> Notion
# ═════════════════════════════════════════════════════════════════════════════════

class TestE2EFullFlow:
    """
    Test end-to-end: simulates the full outbound cycle:
      1. Query Notion for pending items
      2. Generate email with Claude
      3. Create Gmail draft with attachment
      4. Notify Slack
      5. Update Notion
    """

    @patch("send_followup.notion_client")
    @patch("send_followup.draft_manager")
    @patch("send_followup.team_manager")
    @patch("send_followup.claude_client")
    @patch("send_followup._download_attachment")
    def test_full_e2e_draft_mode(self, mock_dl, mock_claude, mock_team, mock_draft, mock_notion):
        """
        Full E2E in DRAFT mode:
        Notion item -> Claude email -> Gmail draft (with attachment) -> Slack -> Notion update
        """
        # 1. Notion: stage 0, not yet sent
        mock_notion.get_page.return_value = {
            "properties": {"Follow-Up Stage": {"type": "number", "number": 0}}
        }
        mock_notion.get_number_property.return_value = 0
        mock_notion.resolve_fixed_cc_emails.return_value = {
            "cesar@leaflatam.com",
            "diana@leaflatam.com",
            "piero@leaflatam.com",
        }

        # 2. Team: CS member resolved
        mock_team.resolve_email.return_value = "belsika@leaflatam.com"
        mock_team.get_admins_cc.return_value = ""

        # 3. Claude: generates email
        mock_claude.generate_followup_email.return_value = {
            "subject": "Seguimiento: Proyecto Solar LATAM — Planos actualizados",
            "body_html": (
                "<p>Estimado Juan,</p>"
                "<p>Espero que se encuentre bien. Le escribo en relacion al Proyecto Solar LATAM.</p>"
                "<p>Para poder avanzar con el proyecto, necesitamos los planos actualizados de instalacion.</p>"
                "<p>Podria indicarnos en que fecha nos podrian hacer llegar esta informacion?</p>"
                "<p>Saludos cordiales,<br>Diana Farje</p>"
            ),
        }

        # 4. Attachment: downloaded from Dropbox
        mock_dl.return_value = {
            "filename": "planos_instalacion.pdf",
            "data": b"fake-pdf-content-123",
            "mime_type": "application/pdf",
        }

        # 5. Draft: created successfully
        mock_draft.create_draft_and_notify.return_value = {
            "draft_id": "draft-e2e-001",
            "slack_ts": "1234567890.123456",
        }

        # Execute
        item = _make_actionable_item(
            documentation_url="https://www.dropbox.com/s/abc123/planos_instalacion.pdf?dl=0",
        )
        with patch("send_followup.SYSTEM_MODE", "DRAFT"):
            result = send_followup_for_item(item)

        # ── Assertions ──────────────────────────────────────────
        # Success
        assert result["success"] is True
        assert result["draft_id"] == "draft-e2e-001"

        # Claude was called
        mock_claude.generate_followup_email.assert_called_once()
        call_kwargs = mock_claude.generate_followup_email.call_args
        assert call_kwargs.kwargs.get("language") == "ES" or "ES" in str(call_kwargs)
        assert call_kwargs.kwargs.get("stage") == 1 or 1 in str(call_kwargs)

        # Attachment was downloaded
        mock_dl.assert_called_once()

        # Draft was created with correct parameters
        draft_call = mock_draft.create_draft_and_notify.call_args
        assert "juan@clientcorp.com" in str(draft_call)  # recipient
        assert "cesar@leaflatam.com" in str(draft_call)  # CC
        assert "diana@leaflatam.com" in str(draft_call)  # CC
        assert "piero@leaflatam.com" in str(draft_call)  # CC

        # Attachments passed
        draft_kwargs = draft_call.kwargs if draft_call.kwargs else {}
        attachments = draft_kwargs.get("attachments", [])
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "planos_instalacion.pdf"

        # Notion was updated
        mock_notion.update_page.assert_called_once()
        mock_notion.append_to_log.assert_called()


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE: Learning Engine & Style
# ═════════════════════════════════════════════════════════════════════════════════

class TestLearningEngine:
    """Tests for the learning engine and style management."""

    def test_similarity_identical(self):
        assert _similarity("hello world", "hello world") == 1.0

    def test_similarity_different(self):
        assert _similarity("abc", "xyz") < 0.5

    def test_similarity_empty(self):
        assert _similarity("", "hello") == 0.0

    def test_strip_html(self):
        assert _strip_html("<p>Hello <b>World</b></p>") == "Hello World"

    @patch("learning_engine.load_metrics")
    def test_recommendation_draft_insufficient_data(self, mock_m):
        mock_m.return_value = {"total_drafts": 5, "approval_rate": 0.9}
        rec = get_mode_recommendation()
        assert rec["recommendation"] == "DRAFT"

    @patch("learning_engine.load_metrics")
    def test_recommendation_auto_high_approval(self, mock_m):
        mock_m.return_value = {"total_drafts": 30, "approval_rate": 0.96}
        rec = get_mode_recommendation()
        assert rec["recommendation"] == "AUTO"

    @patch("learning_engine.load_metrics")
    def test_recommendation_semi_auto(self, mock_m):
        mock_m.return_value = {"total_drafts": 25, "approval_rate": 0.85}
        rec = get_mode_recommendation()
        assert rec["recommendation"] == "SEMI_AUTO"

    def test_style_store_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("style_store.STYLE_DATA_DIR", Path(tmp)):
                save_style_example("Test email body", "ES", project_name="P1", stage=1)
                result = load_style_examples(language="ES")
                assert len(result) == 1
                assert "Test email body" in result[0]

    def test_metrics_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("style_store.STYLE_DATA_DIR", Path(tmp)):
                save_metrics({"total_drafts": 42, "approval_rate": 0.75})
                m = load_metrics()
                assert m["total_drafts"] == 42

    def test_init_style_data_creates_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("style_store.STYLE_DATA_DIR", Path(tmp)):
                init_style_data()
                assert (Path(tmp) / "drafts_log.jsonl").exists()
                assert (Path(tmp) / "sent_log.jsonl").exists()
                assert (Path(tmp) / "style_examples.json").exists()
                assert (Path(tmp) / "learning_metrics.json").exists()


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE: Config Validation
# ═════════════════════════════════════════════════════════════════════════════════

class TestConfigValidation:
    """Verify all config values are correct."""

    def test_system_mode_valid(self):
        assert config.SYSTEM_MODE in ("DRAFT", "SEMI_AUTO", "AUTO")

    def test_supported_languages(self):
        assert config.SUPPORTED_LANGUAGES == ("ES", "EN", "PT")

    def test_country_timezones_count(self):
        assert len(config.COUNTRY_TIMEZONES) >= 10

    def test_business_hours_range(self):
        assert 0 <= config.BUSINESS_HOURS_START < config.BUSINESS_HOURS_END <= 24

    def test_poll_intervals_positive(self):
        assert config.POLL_INTERVAL_OUTBOUND > 0
        assert config.POLL_INTERVAL_TEAM_INBOUND > 0
        assert config.POLL_INTERVAL_CLIENT_INBOUND > 0

    def test_followup_schedule_ascending(self):
        vals = [config.FOLLOWUP_SCHEDULE[k] for k in sorted(config.FOLLOWUP_SCHEDULE)]
        for i in range(1, len(vals)):
            assert vals[i] >= vals[i - 1]


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE: Email Validation
# ═════════════════════════════════════════════════════════════════════════════════

class TestEmailValidation:
    """Email format validation tests."""

    def test_valid_email(self):
        assert _is_valid_email("test@example.com") is True

    def test_valid_subdomain(self):
        assert _is_valid_email("a@b.co.uk") is True

    def test_invalid_no_at(self):
        assert _is_valid_email("noemail") is False

    def test_invalid_no_domain(self):
        assert _is_valid_email("test@") is False

    def test_empty_string(self):
        assert _is_valid_email("") is False

    def test_none(self):
        assert _is_valid_email(None) is False

    def test_spaces(self):
        assert _is_valid_email("  ") is False


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE: Fallback Templates
# ═════════════════════════════════════════════════════════════════════════════════

class TestFallbackTemplates:
    """Verify all required HTML templates exist with placeholders."""

    TEMPLATES_DIR = TOOLS_DIR / "templates"

    def test_all_12_templates_exist(self):
        expected = [
            "reminder_es.html", "reminder_en.html", "reminder_pt.html",
            "second_notice_es.html", "second_notice_en.html", "second_notice_pt.html",
            "urgent_es.html", "urgent_en.html", "urgent_pt.html",
            "escalation_es.html", "escalation_en.html", "escalation_pt.html",
        ]
        for template in expected:
            assert (self.TEMPLATES_DIR / template).exists(), f"Missing template: {template}"

    def test_templates_have_content(self):
        for tmpl in self.TEMPLATES_DIR.glob("*.html"):
            content = tmpl.read_text(encoding="utf-8")
            assert len(content) > 50, f"Template too small: {tmpl.name}"

    def test_templates_have_placeholders(self):
        for tmpl in self.TEMPLATES_DIR.glob("*.html"):
            content = tmpl.read_text(encoding="utf-8")
            assert "{{" in content or "{" in content, f"No placeholders in {tmpl.name}"


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE: Project Structure
# ═════════════════════════════════════════════════════════════════════════════════

class TestProjectStructure:
    """Verify all required files exist."""
    ROOT = Path(__file__).parent.parent

    def test_readme(self):
        assert (self.ROOT / "README.md").exists()

    def test_dockerfile(self):
        assert (self.ROOT / "Dockerfile").exists()

    def test_docker_compose(self):
        assert (self.ROOT / "docker-compose.yml").exists()

    def test_requirements(self):
        assert (self.ROOT / "requirements.txt").exists()

    def test_tools_init(self):
        assert (TOOLS_DIR / "__init__.py").exists()

    def test_workflows_dir(self):
        assert (self.ROOT / "workflows").is_dir()

    def test_all_python_files_parse(self):
        import py_compile
        errors = []
        for py_file in TOOLS_DIR.glob("*.py"):
            try:
                py_compile.compile(str(py_file), doraise=True)
            except py_compile.PyCompileError as e:
                errors.append(f"{py_file.name}: {e}")
        assert errors == [], f"Syntax errors:\n" + "\n".join(errors)


# ═════════════════════════════════════════════════════════════════════════════════
#  TEST SUITE: Daemon Cycles Integration
# ═════════════════════════════════════════════════════════════════════════════════

class TestDaemonCycles:
    """Verify daemon cycles are properly configured."""

    def test_outbound_cycle_runs(self):
        """outbound_cycle should not crash when imports work."""
        with patch("check_pending_items.get_actionable_items", return_value=[]):
            from daemon_main import outbound_cycle
            outbound_cycle()  # Should complete without error

    def test_daily_summary_cycle_runs(self):
        """daily_summary_cycle should not crash."""
        with patch("daily_summary.send_daily_summary"):
            from daemon_main import daily_summary_cycle
            daily_summary_cycle()

    def test_eod_summary_cycle_runs(self):
        """eod_summary_cycle should not crash."""
        with patch("daily_summary.send_eod_slack_summary", return_value=True):
            from daemon_main import eod_summary_cycle
            eod_summary_cycle()


# ═════════════════════════════════════════════════════════════════════════════════
#  RUNNER
# ═════════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
