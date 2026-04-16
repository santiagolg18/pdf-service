"""
Servicio FastAPI para generar el PDF final de una factura aprobada.

Flujo:
  1. Recibe POST /generate-final-pdf con el invoice_id.
  2. Consulta Supabase para traer la factura + approvals + approvers.
  3. Descarga el PDF original desde Supabase Storage.
  4. Genera hoja de constancia con reportlab.
  5. Fusiona original + hoja con PyPDF2.
  6. Sube el PDF final a Storage y actualiza invoices.final_pdf_path.
  7. Responde con la ruta + base64 del PDF (opcional para adjuntar en email).

Deploy:
  uvicorn main:app --host 0.0.0.0 --port 8000

Variables de entorno requeridas:
  SUPABASE_URL       — ej. https://xxxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  — service_role key
  STORAGE_BUCKET     — opcional, default "invoices"
"""

import base64
import datetime
import io
import os

import httpx
from fastapi import FastAPI, HTTPException
from PyPDF2 import PdfMerger
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "invoices")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

app = FastAPI(title="Invoice Approval PDF Service")


class GeneratePDFRequest(BaseModel):
    invoice_id: str


def generate_approval_page(invoice, approvals) -> io.BytesIO:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, topMargin=0.75 * inch, bottomMargin=0.75 * inch
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CustomTitle", parent=styles["Title"], fontSize=16, spaceAfter=20
    )

    elements = [
        Paragraph("CONSTANCIA DE APROBACIÓN DE FACTURA", title_style),
        Spacer(1, 12),
    ]

    info_rows = [
        ["Factura N°:", invoice["invoice_number"]],
        ["Proveedor:", invoice["supplier_name"]],
        ["NIT:", invoice["supplier_nit"]],
        ["Monto Total:", f"${invoice['total_amount']:,.2f} {invoice.get('currency', 'COP')}"],
        ["Fecha de Recepción:", invoice.get("received_at") or "N/A"],
    ]
    info_table = Table(info_rows, colWidths=[2 * inch, 4 * inch])
    info_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 11),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elements.append(info_table)
    elements.append(Spacer(1, 24))
    elements.append(Paragraph("Registro de Aprobaciones:", styles["Heading2"]))
    elements.append(Spacer(1, 8))

    header = ["#", "Aprobador", "Estado", "Fecha y Hora"]
    rows = [header]
    for i, a in enumerate(approvals, 1):
        status_text = "APROBADO" if a["status"] == "approved" else "RECHAZADO"
        date_str = a.get("approved_at") or "Pendiente"
        if date_str != "Pendiente":
            try:
                dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                date_str = dt.strftime("%d/%m/%Y %H:%M:%S")
            except ValueError:
                pass
        rows.append([str(i), a["approver_name"], status_text, date_str])

    approval_table = Table(
        rows, colWidths=[0.5 * inch, 2.5 * inch, 1.5 * inch, 2 * inch]
    )
    approval_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563eb")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    elements.append(approval_table)
    elements.append(Spacer(1, 30))

    gen_date = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    elements.append(
        Paragraph(
            f"<i>Documento generado automáticamente el {gen_date}. "
            f"Este documento certifica que las aprobaciones fueron registradas "
            f"electrónicamente en el sistema.</i>",
            ParagraphStyle("Footer", parent=styles["Normal"], fontSize=9, textColor=colors.grey),
        )
    )

    doc.build(elements)
    buffer.seek(0)
    return buffer


def merge_pdfs(original_bytes: bytes, approval_page: io.BytesIO) -> bytes:
    merger = PdfMerger()
    merger.append(io.BytesIO(original_bytes))
    merger.append(approval_page)
    out = io.BytesIO()
    merger.write(out)
    merger.close()
    return out.getvalue()


async def fetch_invoice_bundle(client: httpx.AsyncClient, invoice_id: str) -> dict:
    query = (
        f"{SUPABASE_URL}/rest/v1/invoices"
        f"?id=eq.{invoice_id}"
        f"&select=*,approvals(*,approvers(name,email))"
    )
    resp = await client.get(query, headers=SB_HEADERS)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} no encontrada")
    return rows[0]


async def download_original_pdf(client: httpx.AsyncClient, storage_path: str) -> bytes:
    url = f"{SUPABASE_URL}/storage/v1/object/{storage_path}"
    resp = await client.get(url, headers=SB_HEADERS)
    if resp.status_code != 200:
        raise HTTPException(status_code=404, detail=f"PDF original no encontrado en {storage_path}")
    return resp.content


async def upload_final_pdf(client: httpx.AsyncClient, path: str, pdf: bytes) -> None:
    url = f"{SUPABASE_URL}/storage/v1/object/{path}"
    resp = await client.post(
        url,
        headers={**SB_HEADERS, "Content-Type": "application/pdf", "x-upsert": "true"},
        content=pdf,
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Upload falló: {resp.status_code} {resp.text}")


async def patch_invoice_final_path(client: httpx.AsyncClient, invoice_id: str, path: str) -> None:
    url = f"{SUPABASE_URL}/rest/v1/invoices?id=eq.{invoice_id}"
    resp = await client.patch(
        url,
        headers={**SB_HEADERS, "Content-Type": "application/json"},
        json={"final_pdf_path": path},
    )
    resp.raise_for_status()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/generate-final-pdf")
async def generate_final_pdf(request: GeneratePDFRequest):
    async with httpx.AsyncClient(timeout=30.0) as client:
        invoice = await fetch_invoice_bundle(client, request.invoice_id)

        if not invoice.get("pdf_storage_path"):
            raise HTTPException(status_code=400, detail="invoices.pdf_storage_path vacío")

        original = await download_original_pdf(client, invoice["pdf_storage_path"])

        approvals_payload = [
            {
                "approver_name": a["approvers"]["name"],
                "status": a["status"],
                "approved_at": a.get("approved_at"),
            }
            for a in invoice.get("approvals", [])
        ]

        approval_page = generate_approval_page(invoice, approvals_payload)
        final_pdf = merge_pdfs(original, approval_page)

        final_path = f"{STORAGE_BUCKET}/final/{invoice['invoice_number']}_aprobado.pdf"
        await upload_final_pdf(client, final_path, final_pdf)
        await patch_invoice_final_path(client, request.invoice_id, final_path)

    return {
        "success": True,
        "final_pdf_path": final_path,
        "pdf_base64": base64.b64encode(final_pdf).decode(),
    }
