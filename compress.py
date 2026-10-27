"""
Compresión de PDF para ahorrar espacio en Storage.

Los PDF que se suben (facturas del correo, órdenes de compra, soportes) y el
PDF aprobado suelen traer imágenes escaneadas a 300 dpi o guardadas sin
compresión. Aquí se bajan a 150 dpi (lo normal para documentos de oficina: se
leen bien en pantalla e impresos) y se limpia lo que sobra del archivo.

Regla de oro: si algo sale mal, o el ahorro no vale la pena, se devuelve None y
quien llama deja el archivo original tal cual. Comprimir nunca puede dañar un
documento.
"""

import io
import logging

import pymupdf
from PIL import Image as PILImage

logger = logging.getLogger("pdf-service")

TARGET_DPI = 150
JPEG_QUALITY = 70

# Por debajo de este ahorro no se reemplaza el archivo: no compensa el riesgo
# de recomprimir algo que ya estaba bien.
MIN_SAVING = 0.05

# Logos e íconos pequeños no valen la pena.
MIN_IMAGE_BYTES = 30_000

# Un escaneo "a color" con menos de esta fracción de píxeles realmente
# coloreados es, en la práctica, gris: en escala de grises pesa la tercera
# parte. El umbral es bajo a propósito: un formato impreso en azul o un
# resaltador amarillo (cientos de veces más) tienen que conservar su color.
MAX_COLOR_FRACTION = 0.002


def _display_dpi(doc: pymupdf.Document) -> dict[int, float]:
    """La resolución a la que se ve cada imagen, por xref.

    Si una imagen se dibuja varias veces, cuenta la vez que se ve más grande
    (la de menor dpi), para no dejarla borrosa en ninguna parte.
    """
    dpi: dict[int, float] = {}
    for page in doc:
        for info in page.get_image_info(xrefs=True):
            xref = info.get("xref")
            width = pymupdf.Rect(info["bbox"]).width
            if not xref or width <= 0:
                continue
            value = info["width"] / (width / 72)
            dpi[xref] = min(dpi.get(xref, value), value)
    return dpi


def _is_grayish(img: PILImage.Image) -> bool:
    """True si casi ningún píxel tiene color de verdad (saturado y no oscuro)."""
    small = img.copy()
    small.thumbnail((400, 400))
    _, sat, val = small.convert("HSV").split()
    colored = PILImage.eval(sat, lambda s: 255 if s > 80 else 0)
    bright = PILImage.eval(val, lambda v: 255 if v > 60 else 0)
    mask = PILImage.composite(colored, PILImage.new("L", small.size, 0), bright)
    return mask.histogram()[255] / (small.width * small.height) < MAX_COLOR_FRACTION


def _shrink_scans(doc: pymupdf.Document) -> None:
    """Baja a TARGET_DPI los JPEG escaneados y pasa a gris los que no tienen color.

    rewrite_images (más abajo) no siempre toca los JPEG de escáner, que son
    justo lo que más pesa en las órdenes de compra.
    """
    for xref, dpi in _display_dpi(doc).items():
        # El filtro puede venir como nombre (/DCTDecode) o como arreglo ([/DCTDecode]).
        if "/DCTDecode" not in doc.xref_get_key(xref, "Filter")[1]:
            continue
        # Con transparencia (SMask) o en CMYK, reemplazarla podría cambiar cómo se ve.
        if doc.xref_get_key(xref, "SMask")[0] != "null":
            continue
        try:
            extracted = doc.extract_image(xref)
            if not extracted or len(extracted["image"]) < MIN_IMAGE_BYTES:
                continue
            img = PILImage.open(io.BytesIO(extracted["image"]))
            img.load()
            if img.mode not in ("RGB", "L"):
                continue
            if img.mode == "RGB" and _is_grayish(img):
                img = img.convert("L")
            if dpi > TARGET_DPI * 1.1:
                scale = TARGET_DPI / dpi
                img = img.resize(
                    (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                    PILImage.LANCZOS,
                )
            out = io.BytesIO()
            img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
            if len(out.getvalue()) < len(extracted["image"]) * 0.9:
                doc[0].replace_image(xref, stream=out.getvalue())
        except Exception as exc:  # noqa: BLE001 — una imagen rara no frena el resto
            logger.warning("No se pudo recomprimir la imagen %s: %s", xref, exc)


def compress_pdf(data: bytes) -> bytes | None:
    """Versión liviana del PDF, o None si no se debe reemplazar el original.

    None cuando: no es un PDF legible, está cifrado, el resultado cambia el
    número de páginas, o no queda al menos MIN_SAVING más liviano.
    """
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — PDF dañado: se deja como está
        logger.warning("No se pudo abrir el PDF para comprimirlo: %s", exc)
        return None

    try:
        if doc.needs_pass or doc.is_encrypted:
            return None
        pages = doc.page_count
        if pages == 0:
            return None

        _shrink_scans(doc)
        # Lo demás (PNG sin comprimir, JPEG de más de 165 dpi...). Las imágenes
        # en blanco y negro se dejan: a 150 dpi el texto escaneado se rompe.
        doc.rewrite_images(
            dpi_threshold=TARGET_DPI + 15,
            dpi_target=TARGET_DPI,
            quality=JPEG_QUALITY,
            bitonal=False,
        )
        try:
            doc.subset_fonts()
        except Exception as exc:  # noqa: BLE001 — opcional
            logger.info("No se pudieron recortar las fuentes: %s", exc)

        result = doc.tobytes(garbage=4, deflate=True, use_objstms=1)
    except Exception as exc:  # noqa: BLE001 — nunca dañar el original
        logger.warning("Falló la compresión del PDF: %s", exc)
        return None
    finally:
        doc.close()

    if len(result) > len(data) * (1 - MIN_SAVING):
        return None
    try:
        check = pymupdf.open(stream=result, filetype="pdf")
        same_pages = check.page_count == pages
        check.close()
    except Exception:  # noqa: BLE001
        return None
    return result if same_pages else None
