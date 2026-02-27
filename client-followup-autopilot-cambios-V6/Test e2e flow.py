#!/usr/bin/env python3
"""
TEST E2E — Flujo completo de Follow-Up
=======================================
Toma el PRIMER item accionable que encuentre en Notion,
overridea sender y receptor a belsika@leaflatam.com,
y prueba todo el flujo V6:

  1. Buscar items accionables en Notion
  2. Resolver documentation_url (adjunto)
  3. Resolver CC fijos (Diana, Piero, César Montes)
  4. Generar email con Claude (sin negritas, sin fecha, firma personal)
  5. Crear draft en Gmail con adjunto
  6. Notificar en Slack

USO:
  cd /app/tools && python3 test_e2e_flow.py
"""

import sys
import os
import json
import logging
import re

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TEST_EMAIL = "ilyra@leaflatam.com"

print("=" * 60)
print("TEST E2E — Flujo completo Follow-Up V6")
print(f"Sender & Receptor override: {TEST_EMAIL}")
print("=" * 60)

# ═════════════════════════════════════════════════════════════
# PASO 1: Buscar items accionables reales
# ═════════════════════════════════════════════════════════════
print(f"\n=== 1. NOTION — Buscar items accionables ===")

from check_pending_items import get_actionable_items
import notion_client
import config

items = get_actionable_items()
print(f"  Items accionables: {len(items)}")

if not items:
    print("  ❌ No hay items accionables.")
    print("  Resetea un item en Notion: Follow-Up Stage → 0, borra Last Follow-Up Date")
    sys.exit(1)

# Usar el primer item
item = items[0]
print(f"\n  Usando: {item['project_name']}")
print(f"  Pendiente:      {item['pending_item']}")
print(f"  Status:         {item['status']}")
print(f"  Stage:          {item['follow_up_stage']} → {item['next_stage']}")
print(f"  Cliente:        {item['client_name']}")
print(f"  Email real:     {item['client_email']} → OVERRIDE: {TEST_EMAIL}")
print(f"  CS Owner:       {item.get('client_success', '?')}")
print(f"  CS Email real:  {item.get('cs_email', '?')} → OVERRIDE: {TEST_EMAIL}")
print(f"  Info pendiente: {item.get('impact_description', '(vacío)')[:100]}")
print(f"  Idioma:         {item['client_language']}")

# ═════════════════════════════════════════════════════════════
# PASO 2: Resolver documentation_url (adjunto)
# ═════════════════════════════════════════════════════════════
print(f"\n=== 2. DOCUMENTACIÓN (adjunto) ===")

doc_url = item.get("documentation_url", "")
if doc_url:
    print(f"  ✅ URL: {doc_url[:80]}...")
else:
    print(f"  ⚠️  Sin URL de documentación (no habrá adjunto)")

# ═════════════════════════════════════════════════════════════
# PASO 3: Resolver CC fijos
# ═════════════════════════════════════════════════════════════
print(f"\n=== 3. CC FIJOS ===")

fixed_cc = notion_client.resolve_fixed_cc_emails()
if fixed_cc:
    for email in sorted(fixed_cc):
        print(f"  ✅ CC: {email}")
else:
    print(f"  ⚠️  No se encontraron CC fijos")

# ═════════════════════════════════════════════════════════════
# PASO 4: Descargar adjunto
# ═════════════════════════════════════════════════════════════
print(f"\n=== 4. DESCARGA DE ADJUNTO ===")

from send_followup import _download_attachment
attachments = []

if doc_url:
    from urllib.parse import urlparse, unquote
    parsed = urlparse(doc_url)
    filename = unquote(parsed.path.split("/")[-1]) or "documento"
    if not any(filename.endswith(ext) for ext in [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".png", ".jpg"]):
        filename = f"{item['project_name']} - Documentación"

    downloaded = _download_attachment(doc_url, filename)
    if downloaded:
        attachments.append(downloaded)
        size_kb = len(downloaded["data"]) / 1024
        print(f"  ✅ Descargado: {downloaded['filename']} ({size_kb:.1f} KB, {downloaded['mime_type']})")
    else:
        print(f"  ❌ Error descargando")
else:
    print(f"  — Sin URL, saltando")

# ═════════════════════════════════════════════════════════════
# PASO 5: Generar email con Claude
# ═════════════════════════════════════════════════════════════
print(f"\n=== 5. CLAUDE — Generar email ===")

import claude_client

next_stage = item["next_stage"]
language = item["client_language"]
sender_name = item.get("client_success", "") or "Client Success"

context = {
    "project_name": item["project_name"],
    "client_name": item["client_name"] or "Cliente",
    "pending_item": item["pending_item"],
    "information_needed": item.get("impact_description") or "Información pendiente del proyecto",
    "impact_description": item.get("impact_description") or "Necesaria para avanzar con el proyecto",
    "follow_up_stage": item["follow_up_stage"],
}

email = claude_client.generate_followup_email(
    context=context,
    language=language,
    stage=next_stage,
    company_name=config.COMPANY_NAME,
    sender_name=sender_name,
)

has_bold = False
has_date = False
has_when = False
has_name = False
has_link = False

if email:
    subject = email["subject"]
    body_html = email["body_html"]

    print(f"  ✅ Email generado")
    print(f"  Asunto: {subject}")

    # Validaciones V6
    has_bold = bool(re.search(r'</?(?:strong|b)>', body_html))
    has_date = bool(re.search(r'antes del \d|before \w+ \d', body_html, re.IGNORECASE))
    has_when = bool(re.search(r'cuándo|cuando|qué fecha|en qué fecha|when can|when could', body_html, re.IGNORECASE))
    first_name = sender_name.split()[0] if sender_name != "Client Success" else ""
    has_name = first_name.lower() in body_html.lower() if first_name else False
    has_link = bool(re.search(r'<a\s+href=', body_html, re.IGNORECASE))

    print(f"\n  --- Validaciones V6 ---")
    print(f"  {'✅' if not has_bold else '❌'} Sin negritas: {'OK' if not has_bold else 'TIENE <strong>/<b>'}")
    print(f"  {'✅' if not has_date else '❌'} Sin fecha límite: {'OK' if not has_date else 'TIENE FECHA'}")
    print(f"  {'✅' if has_when else '⚠️ '} Pregunta cuándo: {'OK' if has_when else 'NO PREGUNTA'}")
    print(f"  {'✅' if has_name else '⚠️ '} Firma personal ({sender_name}): {'OK' if has_name else 'NO ENCONTRADA'}")
    print(f"  {'✅' if not has_link else '❌'} Sin links: {'OK' if not has_link else 'TIENE LINKS'}")

    # Preview
    preview = re.sub(r'<[^>]+>', ' ', body_html)
    preview = re.sub(r'\s+', ' ', preview).strip()
    print(f"\n  --- Preview ---")
    print(f"  {preview[:400]}")
else:
    print(f"  ❌ Claude falló")
    sys.exit(1)

# ═════════════════════════════════════════════════════════════
# PASO 6: Crear draft en Gmail (override sender/recipient)
# ═════════════════════════════════════════════════════════════
print(f"\n=== 6. GMAIL — Crear draft ===")

import gmail_client

sender = TEST_EMAIL
recipient = TEST_EMAIL

# CC: fixed CC but exclude test email
cc_set = set(fixed_cc) if fixed_cc else set()
cc_set.discard(sender)
cc_recipients = ", ".join(sorted(cc_set)) if cc_set else ""

print(f"  Sender:   {sender}")
print(f"  To:       {recipient}")
print(f"  CC:       {cc_recipients or '(ninguno)'}")
print(f"  Adjuntos: {len(attachments)}")
for att in attachments:
    print(f"            📎 {att['filename']} ({att['mime_type']})")

draft_result = None
try:
    draft_result = gmail_client.create_draft(
        to=recipient,
        subject=f"[TEST] {subject}",
        body_html=body_html,
        cc=cc_recipients,
        from_email=sender,
        attachments=attachments,
    )

    if draft_result:
        print(f"  ✅ Draft creado: {draft_result['id']}")
    else:
        print(f"  ❌ create_draft retornó None")
except Exception as e:
    print(f"  ❌ Error: {e}")

# ═════════════════════════════════════════════════════════════
# PASO 7: Notificar en Slack
# ═════════════════════════════════════════════════════════════
print(f"\n=== 7. SLACK — Notificar ===")

slack_ok = False
if draft_result:
    import slack_client

    try:
        body_preview = re.sub(r'<[^>]+>', ' ', body_html)
        body_preview = re.sub(r'\s+', ' ', body_preview).strip()[:300]

        slack_result = slack_client.post_draft_for_review(
            project_name=item["project_name"],
            client_name=item["client_name"] or "Test Client",
            subject=f"[TEST] {subject}",
            body_preview=body_preview,
            draft_id=draft_result["id"],
            language=language,
            recipient_email=recipient,
            sender_email=sender,
            stage=next_stage,
            cc=cc_recipients,
        )

        if slack_result and slack_result.get("ts"):
            print(f"  ✅ Tarjeta enviada (ts: {slack_result['ts']})")
            slack_ok = True
        else:
            print(f"  ⚠️  Resultado: {slack_result}")
    except Exception as e:
        print(f"  ❌ Error: {e}")
else:
    print(f"  — Sin draft, saltando")

# ═════════════════════════════════════════════════════════════
# RESUMEN
# ═════════════════════════════════════════════════════════════
print(f"\n{'=' * 60}")
print("RESUMEN")
print(f"{'=' * 60}")

checks = [
    ("Items accionables en Notion", len(items) > 0),
    ("Documentation URL resuelta", bool(doc_url)),
    ("Adjunto descargado", len(attachments) > 0),
    ("CC fijos resueltos", bool(fixed_cc)),
    ("Email generado por Claude", email is not None),
    ("Sin negritas (<strong>/<b>)", not has_bold),
    ("Sin fecha límite", not has_date),
    ("Pregunta cuándo pueden enviar", has_when),
    ("Firma con nombre personal", has_name),
    ("Sin links (docs van como adjunto)", not has_link),
    ("Draft creado en Gmail", draft_result is not None),
    ("Tarjeta enviada a Slack", slack_ok),
]

passed = sum(1 for _, ok in checks if ok)
for name, ok in checks:
    print(f"  {'✅' if ok else '❌'} {name}")

print(f"\n  {passed}/{len(checks)} checks OK")

if draft_result:
    print(f"\n  📧 Revisa Gmail de {TEST_EMAIL} → Borradores")
    print(f"  💬 Revisa Slack → canal de review")

print(f"\n{'=' * 60}")
