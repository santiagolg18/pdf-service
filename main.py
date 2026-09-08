"""
Servicio FastAPI para generar el PDF final de una factura aprobada.

El PDF final es el EXPEDIENTE COMPLETO de la factura: constancia de carátula,
factura original, orden de compra y soportes, todo en un solo archivo.

Flujo:
  1. Recibe POST /generate-final-pdf con el invoice_id.
  2. Consulta Supabase para traer la factura + approvals + approvers +
     centro de costo + soportes.
  3. Descarga de Storage la factura original, la orden de compra y los soportes,
     y convierte a PDF los que son imágenes (collect_documents).
  4. Genera la hoja de constancia con reportlab: datos de la factura, resumen de
     la revisión de compras, índice del expediente y registro de aprobaciones.
     Siempre ocupa UNA página.
  5. Fusiona constancia + documentos con PyPDF2, en ese orden.
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
import logging
import os
from dataclasses import dataclass
from xml.sax.saxutils import escape

import httpx
from fastapi import FastAPI, HTTPException
from PIL import Image as PILImage
from PyPDF2 import PdfMerger, PdfReader
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Image,
    KeepInFrame,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger("pdf-service")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "invoices")

# Sello de certificación que se estampa en la hoja de constancia.
APPROVAL_STAMP_URL = os.environ.get(
    "APPROVAL_STAMP_URL",
    "https://res.cloudinary.com/dqnsskjfg/image/upload/v1779891594/Aprobacion_factura_aequ86.png",
)
# Caché en memoria del sello para no descargarlo en cada request.
_stamp_cache: bytes | None = None


async def fetch_approval_stamp(client: httpx.AsyncClient) -> bytes | None:
    """Descarga (y cachea) el sello de certificación.

    El sello es decorativo: si la URL cambia, no responde o devuelve algo
    inesperado, devolvemos None y el PDF se genera sin sello.
    """
    global _stamp_cache
    if _stamp_cache is not None:
        return _stamp_cache
    try:
        resp = await client.get(APPROVAL_STAMP_URL)
        resp.raise_for_status()
        _stamp_cache = resp.content
        return _stamp_cache
    except Exception as exc:  # noqa: BLE001 — sello decorativo, nunca debe romper el flujo
        logger.warning("No se pudo descargar el sello de aprobación (%s): %s", APPROVAL_STAMP_URL, exc)
        return None

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

app = FastAPI(title="Invoice Approval PDF Service")


class GeneratePDFRequest(BaseModel):
    invoice_id: str


# Ancho de todas las tablas de la constancia. Los bloques de texto suelto se
# envuelven en una tabla de este mismo ancho (aligned_block) para que queden
# alineados con ellas: calcular el margen a mano no sirve, porque el
# KeepInFrame de más abajo reescala la hoja y el texto no seguiría a las tablas.
CONTENT_WIDTH = 6 * inch


def aligned_block(flowables) -> Table:
    """Envuelve flowables para que arranquen donde arrancan las tablas."""
    table = Table([[f] for f in flowables], colWidths=[CONTENT_WIDTH])
    table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


# Tope de las observaciones de Compras en la constancia. La hoja se encoge sola
# para que todo quepa en una página (KeepInFrame más abajo), pero sin un tope un
# análisis de varias páginas encogería la letra hasta volverla ilegible. ~1.800
# caracteres son unos 20 renglones a 9 pt en 6,5" de ancho: caben con un
# encogimiento leve. El texto completo siempre queda en la plataforma.
MAX_NOTES_CHARS = 1800

TRUNCATION_SUFFIX = " […texto completo en la plataforma]"


def format_previous_amount(invoice) -> str | None:
    """"$1.100.000,00 (subió 12,2%, +$134.000,00)" a partir del valor anterior.

    Devuelve None cuando no hay comparación posible: el campo es opcional y con
    un valor anterior de 0 (o negativo) el porcentaje no significaría nada.
    """
    raw = invoice.get("previous_amount")
    if raw is None:
        return None
    try:
        previous = float(raw)
        current = float(invoice["total_amount"])
    except (TypeError, ValueError):
        return None
    if previous <= 0:
        return None

    base = f"${previous:,.2f}"
    delta = current - previous
    # Diferencias por debajo de un peso son ruido de redondeo, no un cambio real.
    if abs(delta) < 1:
        return f"{base} (se mantiene)"
    verb = "subió" if delta > 0 else "bajó"
    sign = "+" if delta > 0 else "-"
    percent = abs(delta / previous) * 100
    return f"{base} ({verb} {percent:.1f}%, {sign}${abs(delta):,.2f})"


def clean_notes(raw: str | None) -> str | None:
    """Deja las observaciones listas para meterlas en un Paragraph de reportlab."""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    if len(text) > MAX_NOTES_CHARS:
        # Cortar en el último espacio para no partir una palabra por la mitad.
        cut = text[:MAX_NOTES_CHARS]
        space = cut.rfind(" ")
        if space > MAX_NOTES_CHARS // 2:
            cut = cut[:space]
        text = cut.rstrip(" ,.;:") + TRUNCATION_SUFFIX

    # Paragraph interpreta mini-HTML: sin escapar, un "precio < 100 & IVA"
    # escrito por el usuario rompería la generación del PDF entera.
    return escape(text).replace("\n", "<br/>")


def build_review_block(invoice, styles) -> list:
    """Flowables del resumen de la revisión de compras.

    Devuelve [] si la factura no trae ningún dato de revisión, para que las
    facturas anteriores a esta funcionalidad generen su constancia igual que antes.
    """
    cost_center = (invoice.get("cost_centers") or {}).get("name")
    previous_text = format_previous_amount(invoice)
    notes = clean_notes(invoice.get("review_notes"))

    if not cost_center and not previous_text and not notes:
        return []

    elements = [
        Paragraph("Resumen de la Revisión de Compras:", styles["Heading2"]),
        Spacer(1, 8),
    ]

    rows = []
    if cost_center:
        rows.append(["Centro de Costo:", cost_center])
    if previous_text:
        rows.append(["Valor Anterior:", previous_text])
    if rows:
        review_table = Table(rows, colWidths=[2 * inch, 4 * inch])
        review_table.setStyle(
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
        elements.append(review_table)

    if notes:
        label_style = ParagraphStyle(
            "ReviewNotesLabel",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=11,
            spaceBefore=6,
            spaceAfter=4,
        )
        notes_style = ParagraphStyle(
            "ReviewNotes",
            parent=styles["Normal"],
            fontSize=9.5,
            leading=13,
        )
        elements.append(
            aligned_block(
                [Paragraph("Observaciones:", label_style), Paragraph(notes, notes_style)]
            )
        )

    elements.append(Spacer(1, 24))
    return elements


def build_index_block(documents, skipped, styles) -> list:
    """Índice de lo que trae el PDF final, con la página donde empieza cada cosa.

    La constancia siempre ocupa una página (KeepInFrame más abajo), así que el
    primer documento anexado arranca en la página 2.
    """
    if not documents and not skipped:
        return []

    elements = [
        Paragraph("Contenido de este documento:", styles["Heading2"]),
        Spacer(1, 8),
    ]

    rows = []
    page = 2  # 1 es esta misma constancia.
    for doc in documents:
        rows.append([doc.title, f"pág. {page}"])
        page += doc.pages

    if rows:
        index_table = Table(rows, colWidths=[CONTENT_WIDTH - 1.4 * inch, 1.4 * inch])
        index_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica"),
                    ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#e2e8f0")),
                ]
            )
        )
        elements.append(index_table)

    if skipped:
        # Formatos que no se pueden pegar a un PDF (Excel, Word, correos...).
        # Se nombran para que quien audite sepa que existen y dónde buscarlos.
        skipped_style = ParagraphStyle(
            "SkippedAttachments",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
            spaceBefore=8,
            textColor=colors.HexColor("#64748b"),
        )
        nombres = "<br/>".join(f"• {escape(name)}" for name in skipped)
        elements.append(
            aligned_block(
                [
                    Paragraph(
                        f"<b>No anexados</b> (consultar en la plataforma):<br/>{nombres}",
                        skipped_style,
                    )
                ]
            )
        )

    elements.append(Spacer(1, 24))
    return elements


def generate_approval_page(
    invoice,
    approvals,
    stamp_bytes: bytes | None = None,
    documents=None,
    skipped=None,
) -> io.BytesIO:
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

    # Lo que revisó Compras antes de liberar la factura a los aprobadores.
    elements.extend(build_review_block(invoice, styles))

    # Índice del expediente: factura, orden de compra y soportes.
    elements.extend(build_index_block(documents or [], skipped or [], styles))

    elements.append(Paragraph("Registro de Aprobaciones:", styles["Heading2"]))
    elements.append(Spacer(1, 8))

    header = ["#", "Aprobador", "Estado", "Fecha y Hora"]
    rows = [header]
    for i, a in enumerate(approvals, 1):
        status_labels = {
            "approved": "APROBADO",
            "rejected": "RECHAZADO",
            "pending": "PENDIENTE",
        }
        status_text = status_labels.get(a["status"], "PENDIENTE")
        date_str = a.get("approved_at") or "Pendiente"
        if date_str != "Pendiente":
            try:
                dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                colombia_tz = datetime.timezone(datetime.timedelta(hours=-5))
                dt = dt.astimezone(colombia_tz)
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

    if stamp_bytes:
        # Sello de certificación: escalado a 2.5" de ancho conservando proporción.
        # Es decorativo; si los bytes no son una imagen válida, se omite sin romper el PDF.
        try:
            stamp = Image(io.BytesIO(stamp_bytes))
            target_width = 2.5 * inch
            aspect = stamp.imageHeight / float(stamp.imageWidth)
            stamp.drawWidth = target_width
            stamp.drawHeight = target_width * aspect
            stamp.hAlign = "CENTER"
            elements.append(stamp)
            elements.append(Spacer(1, 20))
        except Exception as exc:  # noqa: BLE001 — sello decorativo, nunca debe romper el flujo
            logger.warning("No se pudo renderizar el sello de aprobación: %s", exc)

    colombia_tz = datetime.timezone(datetime.timedelta(hours=-5))
    gen_date = datetime.datetime.now(tz=colombia_tz).strftime("%d/%m/%Y %H:%M:%S")
    elements.append(
        Paragraph(
            f"<i>Documento generado automáticamente el {gen_date}. "
            f"Este documento certifica que las aprobaciones fueron registradas "
            f"electrónicamente en el sistema.</i>",
            ParagraphStyle("Footer", parent=styles["Normal"], fontSize=9, textColor=colors.grey),
        )
    )

    # La constancia SIEMPRE debe ser una sola hoja. KeepInFrame en modo "shrink"
    # escala el contenido hacia abajo si no cabe en el marco, y no lo toca si cabe.
    # Junto con MAX_NOTES_CHARS, garantiza una hoja única y legible por larga que
    # sea la observación que escribió Compras.
    doc.build([KeepInFrame(doc.width, doc.height, elements, mode="shrink")])
    buffer.seek(0)
    return buffer


@dataclass
class AnnexedDocument:
    """Un documento ya convertido a PDF y listo para pegarse al expediente."""

    title: str
    pdf: bytes
    pages: int


# Imágenes que Pillow decodifica sin dependencias extra. heic/heif quedan fuera
# a propósito: necesitarían pillow-heif en la imagen de Docker.
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif"}

# Tope acumulado de lo que se anexa. Cada soporte puede pesar 10 MB y el
# servicio devuelve además el PDF entero en base64 (+33% de memoria); sin tope,
# una factura con muchos anexos podría tumbar el contenedor de Render.
MAX_MERGE_BYTES = 40 * 1024 * 1024


def extension_of(name: str) -> str:
    parts = (name or "").lower().rsplit(".", 1)
    return parts[1] if len(parts) == 2 else ""


def image_to_pdf(data: bytes) -> bytes | None:
    """Convierte una imagen en una página tamaño carta, centrada y sin deformar."""
    try:
        with PILImage.open(io.BytesIO(data)) as img:
            width, height = img.size
        if not width or not height:
            return None

        page_w, page_h = letter
        margin = 0.5 * inch
        max_w, max_h = page_w - 2 * margin, page_h - 2 * margin
        scale = min(max_w / width, max_h / height)
        draw_w, draw_h = width * scale, height * scale

        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=letter)
        pdf.drawImage(
            ImageReader(io.BytesIO(data)),
            (page_w - draw_w) / 2,
            (page_h - draw_h) / 2,
            width=draw_w,
            height=draw_h,
            preserveAspectRatio=True,
            mask="auto",
        )
        pdf.showPage()
        pdf.save()
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 — un soporte ilegible no puede tumbar el PDF
        logger.warning("No se pudo convertir la imagen a PDF: %s", exc)
        return None


def to_annexed_document(title: str, file_name: str, data: bytes) -> AnnexedDocument | None:
    """Deja un archivo listo para anexar, o None si no se puede pegar a un PDF."""
    ext = extension_of(file_name)

    if ext in IMAGE_EXTENSIONS:
        converted = image_to_pdf(data)
        if converted is None:
            return None
        return AnnexedDocument(title=title, pdf=converted, pages=1)

    if ext != "pdf":
        # Excel, Word, ZIP, correos, heic... convertirlos exigiría LibreOffice
        # dentro de la imagen de Docker. Se listan como "no anexados".
        return None

    try:
        pages = len(PdfReader(io.BytesIO(data)).pages)
    except Exception as exc:  # noqa: BLE001 — PDF corrupto o cifrado
        logger.warning("No se pudo leer el PDF '%s': %s", file_name, exc)
        return None
    if pages == 0:
        return None
    return AnnexedDocument(title=title, pdf=data, pages=pages)


async def collect_documents(client: httpx.AsyncClient, invoice) -> tuple[list, list]:
    """Baja y prepara todo el expediente: factura, orden de compra y soportes.

    Devuelve (documentos anexables, nombres de los que no se pudieron anexar).
    Ningún fallo de descarga o de lectura interrumpe la generación del PDF: el
    archivo problemático se reporta como no anexado y el expediente sale igual.
    """
    pendientes = [
        ("Factura original", invoice.get("pdf_storage_path"), "factura.pdf"),
    ]
    if invoice.get("po_storage_path"):
        po_path = invoice["po_storage_path"]
        pendientes.append(("Orden de compra", po_path, po_path))

    soportes = invoice.get("invoice_attachments") or []
    # Cronológico, como se subieron.
    soportes.sort(key=lambda a: a.get("uploaded_at") or "")
    for soporte in soportes:
        nombre = soporte.get("file_name") or "soporte"
        pendientes.append((f"Soporte: {nombre}", soporte.get("storage_path"), nombre))

    documentos: list[AnnexedDocument] = []
    no_anexados: list[str] = []
    acumulado = 0

    for titulo, path, file_name in pendientes:
        etiqueta = titulo.replace("Soporte: ", "")
        if not path:
            no_anexados.append(etiqueta)
            continue

        data = await download_storage_object(client, path)
        if data is None:
            logger.warning("No se pudo descargar '%s' desde %s", etiqueta, path)
            no_anexados.append(etiqueta)
            continue

        if acumulado + len(data) > MAX_MERGE_BYTES:
            logger.warning("'%s' excede el tope de %s bytes; no se anexa", etiqueta, MAX_MERGE_BYTES)
            no_anexados.append(f"{etiqueta} (demasiado grande)")
            continue

        documento = to_annexed_document(titulo, file_name, data)
        if documento is None:
            no_anexados.append(etiqueta)
            continue

        acumulado += len(documento.pdf)
        documentos.append(documento)

    return documentos, no_anexados


def merge_documents(approval_page: io.BytesIO, documents) -> bytes:
    """Une la constancia (de carátula) con el resto del expediente."""
    merger = PdfMerger()
    merger.append(approval_page)
    for documento in documents:
        try:
            merger.append(io.BytesIO(documento.pdf))
        except Exception as exc:  # noqa: BLE001 — ya se validó al leerlo, pero por si acaso
            logger.warning("No se pudo anexar '%s': %s", documento.title, exc)
    out = io.BytesIO()
    merger.write(out)
    merger.close()
    return out.getvalue()


async def fetch_invoice_bundle(client: httpx.AsyncClient, invoice_id: str) -> dict:
    query = (
        f"{SUPABASE_URL}/rest/v1/invoices"
        f"?id=eq.{invoice_id}"
        f"&select=*,cost_centers(name)"
        f",invoice_attachments(file_name,storage_path,mime_type,uploaded_at)"
        f",approvals(*,approvers(name,email))"
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


async def download_storage_object(client: httpx.AsyncClient, storage_path: str) -> bytes | None:
    """Igual que download_original_pdf pero devuelve None en vez de lanzar.

    Los anexos son opcionales: que falte uno no puede impedir que la factura
    tenga su PDF final.
    """
    try:
        resp = await client.get(
            f"{SUPABASE_URL}/storage/v1/object/{storage_path}", headers=SB_HEADERS
        )
    except Exception as exc:  # noqa: BLE001 — red caída, timeout...
        logger.warning("Error descargando %s: %s", storage_path, exc)
        return None
    if resp.status_code != 200:
        return None
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

        # Se baja y se cuenta TODO el expediente antes de dibujar la constancia:
        # el índice necesita saber en qué página empieza cada documento.
        documents, skipped = await collect_documents(client, invoice)
        if not documents:
            raise HTTPException(
                status_code=404,
                detail=f"No se pudo descargar la factura original de {invoice['pdf_storage_path']}",
            )

        approvals_payload = [
            {
                "approver_name": a["approvers"]["name"],
                "status": a["status"],
                "approved_at": a.get("approved_at"),
            }
            for a in invoice.get("approvals", [])
        ]

        stamp_bytes = await fetch_approval_stamp(client)
        approval_page = generate_approval_page(
            invoice, approvals_payload, stamp_bytes, documents, skipped
        )
        final_pdf = merge_documents(approval_page, documents)

        final_path = f"{STORAGE_BUCKET}/aprobadas/{invoice['supplier_nit']}/{invoice['invoice_number']}_aprobada.pdf"
        await upload_final_pdf(client, final_path, final_pdf)
        await patch_invoice_final_path(client, request.invoice_id, final_path)

    return {
        "success": True,
        "final_pdf_path": final_path,
        "pdf_base64": base64.b64encode(final_pdf).decode(),
    }
