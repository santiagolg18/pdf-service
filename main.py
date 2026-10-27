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
     la revisión de compras, compilado de observaciones (Compras y aprobadores),
     índice del expediente y registro de aprobaciones con la firma de cada
     aprobador. Siempre ocupa UNA página.
  5. Fusiona constancia + documentos con PyPDF2, en ese orden, y comprime el
     resultado (compress.py).
  6. Sube el PDF final a Storage y actualiza invoices.final_pdf_path.
  7. Responde con la ruta + base64 del PDF (opcional para adjuntar en email).

Además, POST /compress-pending (lo llama un cron de Supabase) comprime en su
misma ruta los PDF del bucket que aún no se han procesado: facturas del correo,
órdenes de compra, soportes y facturas manuales.

Deploy:
  uvicorn main:app --host 0.0.0.0 --port 8000

Variables de entorno requeridas:
  SUPABASE_URL       — ej. https://xxxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  — service_role key
  STORAGE_BUCKET     — opcional, default "invoices"
  COMPRESS_SECRET    — clave de /compress-pending (en Vault: compress_secret)
"""

import asyncio
import base64
import datetime
import io
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote
from xml.sax.saxutils import escape

import httpx
from fastapi import FastAPI, Header, HTTPException
from PIL import Image as PILImage
from PIL import ImageOps
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

from compress import TARGET_DPI, compress_pdf

logger = logging.getLogger("pdf-service")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "invoices")
# Clave que debe traer quien llama a /compress-pending (el cron de Supabase).
# Sin ella configurada el endpoint queda apagado.
COMPRESS_SECRET = os.environ.get("COMPRESS_SECRET", "")

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


# Tope TOTAL del compilado de observaciones (Compras + aprobadores) en la
# constancia. La hoja se encoge sola para que todo quepa en una página
# (KeepInFrame más abajo), pero sin un tope varias notas largas encogerían la
# letra hasta volverla ilegible. ~2.400 caracteres son unos 26 renglones a
# 9,5 pt en 6" de ancho: caben con un encogimiento leve. El texto completo
# siempre queda en la plataforma.
MAX_NOTES_CHARS = 2400

# Si lo que queda del tope no alcanza para una nota útil, las que faltan se
# cuentan en vez de mostrarse cortadas a unas pocas palabras.
MIN_NOTE_CHARS = 200

TRUNCATION_SUFFIX = " […texto completo en la plataforma]"

COLOMBIA_TZ = datetime.timezone(datetime.timedelta(hours=-5))

APPROVAL_VERBS = {"approved": "Aprobó", "rejected": "Rechazó"}


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


def clean_notes(raw: str | None, limit: int = MAX_NOTES_CHARS) -> str | None:
    """Deja una observación lista para meterla en un Paragraph de reportlab."""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    if len(text) > limit:
        # Cortar en el último espacio para no partir una palabra por la mitad.
        cut = text[:limit]
        space = cut.rfind(" ")
        if space > limit // 2:
            cut = cut[:space]
        text = cut.rstrip(" ,.;:") + TRUNCATION_SUFFIX

    # Paragraph interpreta mini-HTML: sin escapar, un "precio < 100 & IVA"
    # escrito por el usuario rompería la generación del PDF entera.
    return escape(text).replace("\n", "<br/>")


def build_review_block(invoice, styles) -> list:
    """Flowables del resumen de la revisión de compras.

    Devuelve [] si la factura no trae ningún dato de revisión, para que las
    facturas anteriores a esta funcionalidad generen su constancia igual que antes.
    Las observaciones de Compras van en el compilado (build_comments_block).
    """
    cost_center = (invoice.get("cost_centers") or {}).get("name")
    previous_text = format_previous_amount(invoice)

    rows = []
    if cost_center:
        rows.append(["Centro de Costo:", cost_center])
    if previous_text:
        rows.append(["Valor Anterior:", previous_text])
    if not rows:
        return []

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
    return [
        Paragraph("Resumen de la Revisión de Compras:", styles["Heading2"]),
        Spacer(1, 8),
        review_table,
        Spacer(1, 24),
    ]


def format_colombia_datetime(raw: str | None, fmt: str = "%d/%m/%Y %H:%M:%S") -> str | None:
    """Fecha ISO de Supabase → texto en hora de Colombia (UTC-5).

    Devuelve None si no hay fecha, y el texto tal cual si no se puede interpretar.
    """
    if not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return dt.astimezone(COLOMBIA_TZ).strftime(fmt)


def collect_comments(invoice) -> list[dict]:
    """Todo lo que se escribió sobre la factura.

    Mismo orden que el ícono de comentarios de la plataforma
    (lib/invoices/comments.ts): primero la observación de Compras y luego las
    notas de los aprobadores en el orden en que decidieron.
    """
    comments = []

    review_notes = (invoice.get("review_notes") or "").strip()
    if review_notes:
        reviewer = (invoice.get("reviewer") or {}).get("name")
        comments.append(
            {
                "author": reviewer or "Compras",
                "role": "Compras",
                "at": invoice.get("reviewed_at"),
                "text": review_notes,
            }
        )

    noted = [a for a in invoice.get("approvals") or [] if (a.get("notes") or "").strip()]
    # Las que aún no tienen fecha de decisión, al final.
    noted.sort(key=lambda a: (a.get("approved_at") is None, a.get("approved_at") or ""))
    for a in noted:
        comments.append(
            {
                "author": (a.get("approvers") or {}).get("name") or "—",
                "role": APPROVAL_VERBS.get(a.get("status"), "Aprobador"),
                "at": a.get("approved_at"),
                "text": a["notes"].strip(),
            }
        )

    return comments


def build_comments_block(comments, styles) -> list:
    """Compilado de observaciones: quién escribió, en qué papel, cuándo y qué.

    Devuelve [] si nadie escribió nada, para que la hoja quede como antes.
    """
    if not comments:
        return []

    header_style = ParagraphStyle(
        "CommentHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9.5,
        leading=12,
        spaceAfter=2,
    )
    text_style = ParagraphStyle(
        "CommentText",
        parent=styles["Normal"],
        fontSize=9.5,
        leading=13,
        spaceAfter=6,
    )

    rows = []
    remaining = MAX_NOTES_CHARS
    for i, comment in enumerate(comments):
        if remaining < MIN_NOTE_CHARS:
            left = len(comments) - i
            more = "1 observación más" if left == 1 else f"{left} observaciones más"
            rows.append(
                [Paragraph(f"<i>… y {more} (consultar en la plataforma).</i>", text_style)]
            )
            break

        label = comment["author"]
        if comment["author"] != comment["role"]:
            label = f"{label} — {comment['role']}"
        date = format_colombia_datetime(comment["at"], "%d/%m/%Y %H:%M")
        if date:
            label = f"{label} · {date}"

        rows.append(
            [
                Paragraph(escape(label), header_style),
                Paragraph(clean_notes(comment["text"], remaining), text_style),
            ]
        )
        remaining -= min(len(comment["text"]), remaining)

    return [
        Paragraph("Observaciones:", styles["Heading2"]),
        Spacer(1, 4),
        aligned_block(rows),
        Spacer(1, 18),
    ]


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


APPROVAL_STATUS_LABELS = {"approved": "APROBADO", "rejected": "RECHAZADO"}

# Caja de la firma en la tabla de aprobaciones (unos 3,4 × 1 cm). Pequeña a
# propósito: la constancia tiene que caber en una hoja con todos los aprobadores.
SIGNATURE_MAX_WIDTH = 1.35 * inch
SIGNATURE_MAX_HEIGHT = 0.40 * inch


def signature_flowable(data: bytes | None):
    """La firma del aprobador, escalada para caber en su caja de la tabla.

    La app ya la entrega recortada, sin fondo y en negro (PNG transparente);
    aquí solo se vuelve a recortar al trazo visible por si acaso. Si la imagen
    no sirve se omite: una firma dañada nunca puede romper la constancia.
    """
    if not data:
        return None
    try:
        img = PILImage.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGBA")
        bbox = img.getchannel("A").getbbox()
        if not bbox:
            return None
        img = img.crop(bbox)
        png = io.BytesIO()
        img.save(png, format="PNG")
        png.seek(0)
        scale = min(SIGNATURE_MAX_WIDTH / img.width, SIGNATURE_MAX_HEIGHT / img.height)
        return Image(png, width=img.width * scale, height=img.height * scale)
    except Exception as exc:  # noqa: BLE001 — firma opcional, nunca debe romper el flujo
        logger.warning("No se pudo usar la imagen de una firma: %s", exc)
        return None


def plural(n: int, singular: str, plural_form: str) -> str:
    return f"{n} {singular if n == 1 else plural_form}"


def approval_rule_text(invoice, total: int) -> str:
    """La regla con la que se aprobó, para que nadie lea un "falta uno"."""
    required = min(max(int(invoice.get("required_approvals") or 1), 1), max(total, 1))
    if invoice.get("approval_mode") == "sequential":
        rule = f"debían aprobar {'el aprobador' if total == 1 else f'los {total} aprobadores, en orden'}"
    elif total <= 1:
        rule = "se requería 1 aprobación"
    elif required < total:
        rule = (
            f"se requería {plural(required, 'aprobación', 'aprobaciones')} de {total} "
            f"(cualquiera de los aprobadores)"
        )
    else:
        rule = f"se requerían las {total} aprobaciones"
    return f"<b>Regla:</b> {rule}. <b>Requisito cumplido.</b>"


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
        [
            "Fecha de Recepción:",
            format_colombia_datetime(invoice.get("received_at"), "%d/%m/%Y %H:%M") or "N/A",
        ],
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

    # Todo lo que escribieron Compras y los aprobadores sobre esta factura.
    elements.extend(build_comments_block(collect_comments(invoice), styles))

    # Índice del expediente: factura, orden de compra y soportes.
    elements.extend(build_index_block(documents or [], skipped or [], styles))

    elements.append(Paragraph("Registro de Aprobaciones:", styles["Heading2"]))
    elements.append(Spacer(1, 4))
    elements.append(
        Paragraph(
            approval_rule_text(invoice, len(approvals)),
            ParagraphStyle("Rule", parent=styles["Normal"], fontSize=10),
        )
    )
    elements.append(Spacer(1, 8))

    name_style = ParagraphStyle(
        "ApproverName", parent=styles["Normal"], fontSize=10, leading=12, alignment=1
    )
    no_signature_style = ParagraphStyle(
        "NoSignature",
        parent=styles["Normal"],
        fontSize=8,
        leading=10,
        alignment=1,
        textColor=colors.grey,
    )

    header = ["#", "Aprobador", "Estado", "Firma", "Fecha y Hora"]
    rows = [header]
    for i, a in enumerate(approvals, 1):
        # La constancia solo se genera con la factura ya aprobada: quien no
        # decidió no tenía nada pendiente, el umbral se cumplió sin su firma.
        status_text = APPROVAL_STATUS_LABELS.get(a["status"], "NO REQUERIDA")
        date_str = format_colombia_datetime(a.get("approved_at")) or "—"
        if a["status"] == "approved":
            signature = signature_flowable(a.get("signature_bytes")) or Paragraph(
                "Sin firma registrada", no_signature_style
            )
        else:
            signature = "—"
        rows.append(
            [
                str(i),
                Paragraph(escape(a["approver_name"]), name_style),
                status_text,
                signature,
                date_str,
            ]
        )

    # Suman las mismas 6,5" de antes: la columna de la firma salió del nombre
    # y del estado.
    approval_table = Table(
        rows, colWidths=[0.35 * inch, 1.95 * inch, 1.2 * inch, 1.5 * inch, 1.5 * inch]
    )
    approval_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563eb")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                # La firma ya es alta: menos relleno para que la fila no crezca de más.
                ("TOPPADDING", (3, 1), (3, -1), 3),
                ("BOTTOMPADDING", (3, 1), (3, -1), 3),
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

    gen_date = datetime.datetime.now(tz=COLOMBIA_TZ).strftime("%d/%m/%Y %H:%M:%S")
    elements.append(
        Paragraph(
            f"<i>Documento generado automáticamente el {gen_date}. "
            f"Este documento certifica que las aprobaciones fueron registradas "
            f"electrónicamente en el sistema. La firma que aparece es la imagen "
            f"registrada por cada aprobador en el sistema al momento de aprobar.</i>",
            ParagraphStyle("Footer", parent=styles["Normal"], fontSize=9, textColor=colors.grey),
        )
    )

    # La constancia SIEMPRE debe ser una sola hoja. KeepInFrame en modo "shrink"
    # escala el contenido hacia abajo si no cabe en el marco, y no lo toca si cabe.
    # Junto con MAX_NOTES_CHARS, garantiza una hoja única y legible por largas que
    # sean las observaciones de Compras y de los aprobadores.
    doc.build([KeepInFrame(doc.width, doc.height, elements, mode="shrink")])
    buffer.seek(0)
    return buffer


@dataclass
class AnnexedDocument:
    """Un documento ya convertido a PDF y listo para pegarse al expediente."""

    title: str
    pdf: bytes
    pages: int


# Imágenes que Pillow decodifica sin dependencias extra (MPO es el JPEG de
# algunos celulares). heic/heif quedan fuera a propósito: necesitarían
# pillow-heif en la imagen de Docker.
IMAGE_FORMATS = {"JPEG", "MPO", "PNG", "GIF", "WEBP", "BMP", "TIFF"}

ORIGINAL_TITLE = "Factura original"

# Tope acumulado de lo que se anexa. Cada soporte puede pesar 10 MB y el
# servicio devuelve además el PDF entero en base64 (+33% de memoria); sin tope,
# una factura con muchos anexos podría tumbar el contenedor de Render.
MAX_MERGE_BYTES = 40 * 1024 * 1024


def sniff_kind(data: bytes) -> str | None:
    """"pdf" o "image" según el CONTENIDO del archivo; None si no es anexable.

    No se mira el nombre: una factura cargada a mano puede ser una foto, y un
    archivo puede venir con la extensión equivocada. Excel, Word, ZIP, correos y
    heic caen en None y se listan como "no anexados".
    """
    # La especificación admite basura antes de la cabecera, dentro del primer KB.
    if b"%PDF-" in data[:1024]:
        return "pdf"
    try:
        with PILImage.open(io.BytesIO(data)) as img:
            return "image" if img.format in IMAGE_FORMATS else None
    except Exception:  # noqa: BLE001 — no es una imagen que Pillow entienda
        return None


def storage_segment(value) -> str:
    """Segmento seguro para una key de Storage.

    Storage rechaza la key entera ("Invalid key") si trae símbolos como # % ° o
    tildes, y un número de factura digitado a mano puede traer cualquiera. Los
    espacios se conservan: son válidos y los PDF ya generados los tienen.
    """
    return re.sub(r"[^\w.\- ]+", "_", str(value), flags=re.ASCII).strip() or "_"


# Fotos y pantallazos que se anexan como página: un poco más de calidad que en
# los escaneos (compress.py) porque suelen traer letra pequeña.
PHOTO_JPEG_QUALITY = 80


def image_to_pdf(data: bytes) -> bytes | None:
    """Convierte una imagen en una página tamaño carta, centrada y sin deformar.

    La imagen se reduce a TARGET_DPI del tamaño al que se dibuja: una foto de
    celular de 12 megapíxeles no necesita más de ~1.100 × 1.500 píxeles en una
    hoja carta, y pegarla entera inflaba el PDF aprobado.
    """
    try:
        with PILImage.open(io.BytesIO(data)) as img:
            # Las fotos de celular guardan la rotación en EXIF en vez de girar
            # los píxeles: sin esto la factura saldría acostada.
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                # JPEG no tiene transparencia: fondo blanco, como el papel.
                rgba = img.convert("RGBA")
                img = PILImage.new("RGB", rgba.size, "white")
                img.paste(rgba, mask=rgba.getchannel("A"))
            elif img.mode != "L":
                img = img.convert("RGB")
            width, height = img.size
            if not width or not height:
                return None

            page_w, page_h = letter
            margin = 0.5 * inch
            max_w, max_h = page_w - 2 * margin, page_h - 2 * margin
            scale = min(max_w / width, max_h / height)
            draw_w, draw_h = width * scale, height * scale

            target_w = round(draw_w / 72 * TARGET_DPI)
            if width > target_w:
                target_h = max(1, round(height * target_w / width))
                img = img.resize((target_w, target_h), PILImage.LANCZOS)

            jpeg = io.BytesIO()
            img.save(jpeg, "JPEG", quality=PHOTO_JPEG_QUALITY, optimize=True)

        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=letter)
        pdf.drawImage(
            ImageReader(io.BytesIO(jpeg.getvalue())),
            (page_w - draw_w) / 2,
            (page_h - draw_h) / 2,
            width=draw_w,
            height=draw_h,
            preserveAspectRatio=True,
        )
        pdf.showPage()
        pdf.save()
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 — un soporte ilegible no puede tumbar el PDF
        logger.warning("No se pudo convertir la imagen a PDF: %s", exc)
        return None


def to_annexed_document(title: str, file_name: str, data: bytes) -> AnnexedDocument | None:
    """Deja un archivo listo para anexar, o None si no se puede pegar a un PDF."""
    kind = sniff_kind(data)

    if kind == "image":
        converted = image_to_pdf(data)
        if converted is None:
            return None
        return AnnexedDocument(title=title, pdf=converted, pages=1)

    if kind != "pdf":
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


async def collect_documents(
    client: httpx.AsyncClient, invoice
) -> tuple[list, list, str | None]:
    """Baja y prepara todo el expediente: factura, orden de compra y soportes.

    Devuelve (documentos anexables, nombres de los que no se pudieron anexar,
    problema con la factura original). Un anexo que falla al bajar o al leerse
    no interrumpe la generación: se reporta como no anexado y el expediente sale
    igual. La factura original es la excepción: si falta, el tercer valor dice
    por qué ("missing" o "unreadable") y quien llama decide no generar.
    """
    original_path = invoice.get("pdf_storage_path")
    pendientes = [
        # El nombre es la ruta real: una factura cargada a mano puede ser una
        # foto. Con un "factura.pdf" fijo las imágenes se leían como PDF.
        (ORIGINAL_TITLE, original_path, original_path or ""),
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
    problema_original: str | None = None
    acumulado = 0

    for titulo, path, file_name in pendientes:
        etiqueta = titulo.replace("Soporte: ", "")
        es_original = titulo == ORIGINAL_TITLE
        if not path:
            no_anexados.append(etiqueta)
            if es_original:
                problema_original = "missing"
            continue

        data = await download_storage_object(client, path)
        if data is None:
            logger.warning("No se pudo descargar '%s' desde %s", etiqueta, path)
            no_anexados.append(etiqueta)
            if es_original:
                problema_original = "missing"
            continue

        if acumulado + len(data) > MAX_MERGE_BYTES:
            logger.warning("'%s' excede el tope de %s bytes; no se anexa", etiqueta, MAX_MERGE_BYTES)
            no_anexados.append(f"{etiqueta} (demasiado grande)")
            if es_original:
                problema_original = "unreadable"
            continue

        documento = to_annexed_document(titulo, file_name, data)
        if documento is None:
            no_anexados.append(etiqueta)
            if es_original:
                problema_original = "unreadable"
            continue

        acumulado += len(documento.pdf)
        documentos.append(documento)

    return documentos, no_anexados, problema_original


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
        # invoices tiene varias llaves hacia approvers: hay que decir cuál.
        f",reviewer:approvers!invoices_reviewed_by_fkey(name)"
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


async def fetch_signatures(client: httpx.AsyncClient, approvals) -> dict[str, bytes | None]:
    """Descarga en paralelo las firmas de quienes aprobaron, por ruta.

    Cada aprobación guarda la firma que tenía la persona AL APROBAR
    (approvals.signature_path, la copia un trigger de la base), no la vigente:
    si la cambia después, sus constancias anteriores no cambian. En paralelo
    porque la app corta la llamada a los 30 s.
    """
    paths = sorted(
        {a["signature_path"] for a in approvals if a["status"] == "approved" and a.get("signature_path")}
    )
    images = await asyncio.gather(*(download_storage_object(client, p) for p in paths))
    return dict(zip(paths, images))


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
        documents, skipped, original_problem = await collect_documents(client, invoice)
        # El expediente nunca sale sin la factura: antes, con una OC anexada, se
        # generaba igual y nadie notaba que faltaba el documento principal.
        if original_problem == "missing":
            raise HTTPException(
                status_code=404,
                detail=f"No se encontró el archivo de la factura original en {invoice['pdf_storage_path']}",
            )
        if original_problem:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"El archivo de la factura original ({invoice['pdf_storage_path']}) "
                    "no es un PDF ni una imagen legible"
                ),
            )

        approvals_payload = [
            {
                "approver_name": a["approvers"]["name"],
                "status": a["status"],
                "approved_at": a.get("approved_at"),
                "signature_path": a.get("signature_path"),
            }
            for a in sorted(
                invoice.get("approvals", []),
                key=lambda a: (a.get("approval_order") or 0, a.get("created_at") or ""),
            )
        ]

        signatures = await fetch_signatures(client, approvals_payload)
        for a in approvals_payload:
            a["signature_bytes"] = signatures.get(a["signature_path"])

        stamp_bytes = await fetch_approval_stamp(client)
        approval_page = generate_approval_page(
            invoice, approvals_payload, stamp_bytes, documents, skipped
        )
        final_pdf = merge_documents(approval_page, documents)
        # El expediente repite la factura, la OC y los soportes: comprimido
        # ocupa bastante menos en Storage. Si no vale la pena, va tal cual.
        final_pdf = await run_compress(final_pdf) or final_pdf

        final_path = (
            f"{STORAGE_BUCKET}/aprobadas/{storage_segment(invoice['supplier_nit'])}"
            f"/{storage_segment(invoice['invoice_number'])}_aprobada.pdf"
        )
        await upload_final_pdf(client, final_path, final_pdf)
        await patch_invoice_final_path(client, request.invoice_id, final_path)

    return {
        "success": True,
        "final_pdf_path": final_path,
        "pdf_base64": base64.b64encode(final_pdf).decode(),
    }


# PyMuPDF no admite trabajar en varios hilos a la vez: toda compresión pasa por
# este único hilo. Además así no se bloquea el event loop mientras comprime, y
# /generate-final-pdf sigue respondiendo durante un barrido.
_compress_executor = ThreadPoolExecutor(max_workers=1)


async def run_compress(data: bytes) -> bytes | None:
    return await asyncio.get_running_loop().run_in_executor(_compress_executor, compress_pdf, data)


# Un barrido a la vez: si el cron vuelve a llamar mientras otro sigue, se salta.
_sweep_lock = asyncio.Lock()

SWEEP_MAX_ITEMS = 40
SWEEP_MAX_SECONDS = 50


async def log_compression(client: httpx.AsyncClient, row: dict) -> None:
    resp = await client.post(
        f"{SUPABASE_URL}/rest/v1/storage_compression_log",
        headers={
            **SB_HEADERS,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json={**row, "processed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
    )
    resp.raise_for_status()


async def compress_object(client: httpx.AsyncClient, name: str, size: int | None) -> dict:
    """Comprime un objeto del bucket en su misma ruta. Devuelve la fila del registro."""
    row = {"object_name": name, "original_bytes": size, "final_bytes": None, "error": None}
    path = f"{STORAGE_BUCKET}/{quote(name, safe='/')}"
    data = await download_storage_object(client, path)
    if data is None:
        return {**row, "status": "error", "error": "No se pudo descargar"}
    row["original_bytes"] = len(data)

    smaller = await run_compress(data)
    if smaller is None:
        return {**row, "status": "sin_cambio", "final_bytes": len(data)}

    try:
        await upload_final_pdf(client, path, smaller)
    except HTTPException as exc:
        return {**row, "status": "error", "error": str(exc.detail)[:500]}

    # Los soportes guardan su tamaño en la base y la plataforma lo muestra.
    await client.patch(
        f"{SUPABASE_URL}/rest/v1/invoice_attachments",
        params={"storage_path": f"eq.{STORAGE_BUCKET}/{name}"},
        headers={**SB_HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"},
        json={"size_bytes": len(smaller)},
    )
    return {**row, "status": "comprimido", "final_bytes": len(smaller)}


@app.post("/compress-pending")
async def compress_pending(x_compress_secret: str = Header(default="")):
    """Comprime una tanda de PDF del bucket que aún no se han procesado.

    Lo llama un cron de Supabase cada 10 minutos. Cubre todo lo que se sube
    (correo, Compras, facturas manuales) sin tocar esos flujos: los archivos se
    reemplazan en su misma ruta, así que la base no cambia.
    """
    if not COMPRESS_SECRET or x_compress_secret != COMPRESS_SECRET:
        raise HTTPException(status_code=401, detail="No autorizado")
    if _sweep_lock.locked():
        return {"ocupado": True, "procesados": 0}

    async with _sweep_lock:
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/storage_compression_pending",
                headers={**SB_HEADERS, "Content-Type": "application/json"},
                json={"max_items": SWEEP_MAX_ITEMS},
            )
            resp.raise_for_status()
            batch = resp.json()

            processed = 0
            saved = 0
            errors = 0
            for item in batch:
                if time.monotonic() - started > SWEEP_MAX_SECONDS:
                    break
                try:
                    row = await compress_object(client, item["name"], item.get("size"))
                except Exception as exc:  # noqa: BLE001 — un archivo no frena la tanda
                    logger.warning("Falló la compresión de %s: %s", item["name"], exc)
                    row = {
                        "object_name": item["name"],
                        "original_bytes": item.get("size"),
                        "final_bytes": None,
                        "status": "error",
                        "error": str(exc)[:500],
                    }
                await log_compression(client, row)
                processed += 1
                if row["status"] == "error":
                    errors += 1
                elif row["original_bytes"] and row["final_bytes"]:
                    saved += row["original_bytes"] - row["final_bytes"]

    return {
        "procesados": processed,
        "errores": errors,
        "ahorrado_bytes": saved,
        "quedan_mas": len(batch) == SWEEP_MAX_ITEMS or processed < len(batch),
    }
