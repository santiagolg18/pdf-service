"""
Mide compress_pdf sobre una carpeta de PDF reales, sin subir nada.

Uso (desde la carpeta pdf-service):
  python scripts/benchmark.py <carpeta con PDF> [--muestra 100] [--salida <carpeta>]

Con --salida guarda las versiones comprimidas para abrirlas y compararlas a ojo.
"""

import argparse
import pathlib
import random
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from compress import compress_pdf  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("carpeta")
parser.add_argument("--muestra", type=int, default=0)
parser.add_argument("--salida")
args = parser.parse_args()

archivos = sorted(pathlib.Path(args.carpeta).rglob("*.pdf"))
if args.muestra:
    random.seed(1)
    archivos = random.sample(archivos, min(args.muestra, len(archivos)))

total_antes = total_despues = 0
sin_cambio = 0
peor_tiempo = 0.0
for ruta in archivos:
    data = ruta.read_bytes()
    inicio = time.time()
    resultado = compress_pdf(data)
    tiempo = time.time() - inicio
    peor_tiempo = max(peor_tiempo, tiempo)
    final = resultado if resultado is not None else data
    if resultado is None:
        sin_cambio += 1
    total_antes += len(data)
    total_despues += len(final)
    ahorro = 100 - 100 * len(final) / len(data)
    print(f"{str(ruta.relative_to(args.carpeta))[:60]:60} {len(data)//1024:6} KB -> {len(final)//1024:6} KB  {ahorro:5.1f}%  {tiempo:4.1f}s")
    if args.salida and resultado is not None:
        destino = pathlib.Path(args.salida) / ruta.relative_to(args.carpeta)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(resultado)

print(
    f"\n{len(archivos)} archivos · {total_antes/1048576:.1f} MB -> {total_despues/1048576:.1f} MB"
    f" ({100 - 100*total_despues/max(total_antes,1):.1f}% menos) · sin cambio: {sin_cambio}"
    f" · archivo más lento: {peor_tiempo:.1f}s"
)
