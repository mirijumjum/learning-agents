#!/usr/bin/env python3
"""Convierte sprites escritos como texto en PNG. Sin dependencias: solo la biblioteca estándar.

    python tools/sprite.py            # todos los estudio/sprites/*.txt
    python tools/sprite.py jugador    # solo uno

La paleta está en estudio/paleta.txt (una letra por color, `.` es transparente):

    a = #1a1c2c
    b = #f4f4f4

Cada sprite es un archivo estudio/sprites/<nombre>.txt con una rejilla de letras. Los fotogramas de una
animación se separan con una línea en blanco y se colocan en una tira horizontal (hoja de sprites).
Las líneas que empiezan por `#` son comentarios.

Salida:
    godot/assets/sprites/<nombre>.png         tamaño real, para el juego
    estudio/informes/arte/<nombre>_x8.png     ampliado, para que lo revise el director de arte
    estudio/informes/arte/paleta_x8.png       la paleta en muestras
"""

from __future__ import annotations

import re
import struct
import sys
import zlib
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PALETA = RAIZ / "estudio" / "paleta.txt"
SPRITES = RAIZ / "estudio" / "sprites"
SALIDA = RAIZ / "godot" / "assets" / "sprites"
REVISION = RAIZ / "estudio" / "informes" / "arte"
AMPLIACION = 8

Color = tuple[int, int, int, int]
TRANSPARENTE: Color = (0, 0, 0, 0)


def escribir_png(ruta: Path, pixeles: list[list[Color]]) -> None:
    alto, ancho = len(pixeles), len(pixeles[0])
    crudo = b"".join(b"\x00" + bytes(c for px in fila for c in px) for fila in pixeles)

    def bloque(tipo: bytes, datos: bytes) -> bytes:
        return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos))

    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + bloque(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 6, 0, 0, 0))
                     + bloque(b"IDAT", zlib.compress(crudo, 9))
                     + bloque(b"IEND", b""))


def ampliar(pixeles: list[list[Color]], factor: int) -> list[list[Color]]:
    return [[px for px in fila for _ in range(factor)] for fila in pixeles for _ in range(factor)]


def leer_paleta() -> dict[str, Color]:
    if not PALETA.exists():
        sys.exit(f"✘ Falta {PALETA.relative_to(RAIZ)} (la crea el director de arte).")
    paleta: dict[str, Color] = {".": TRANSPARENTE}
    for n, linea in enumerate(PALETA.read_text(encoding="utf-8").splitlines(), 1):
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        # «a = #1a1c2c  resto ignorado» (con 8 cifras, las dos últimas son la opacidad)
        m = re.match(r"^([^\s#.=])\s*=\s*#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?(?![0-9a-fA-F])", linea)
        if not m:
            print(f"  ⚠ paleta.txt línea {n} no entendida: {linea}")
            continue
        r, g, b = (int(m.group(2)[i:i + 2], 16) for i in (0, 2, 4))
        paleta[m.group(1)] = (r, g, b, int(m.group(3), 16) if m.group(3) else 255)
    return paleta


def leer_fotogramas(ruta: Path) -> list[list[str]]:
    fotogramas: list[list[str]] = [[]]
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        if linea.startswith("#"):
            continue
        linea = linea.rstrip()
        if not linea:
            if fotogramas[-1]:
                fotogramas.append([])
            continue
        fotogramas[-1].append(linea.replace(" ", ""))
    return [f for f in fotogramas if f]


def convertir(ruta: Path, paleta: dict[str, Color]) -> list[str]:
    """Devuelve la lista de errores (vacía si todo va bien)."""
    fotogramas = leer_fotogramas(ruta)
    if not fotogramas:
        return ["está vacío"]
    alto, ancho = len(fotogramas[0]), len(fotogramas[0][0])
    errores = []
    for i, f in enumerate(fotogramas, 1):
        if len(f) != alto:
            errores.append(f"fotograma {i}: {len(f)} filas, se esperaban {alto}")
        for j, fila in enumerate(f, 1):
            if len(fila) != ancho:
                errores.append(f"fotograma {i}, fila {j}: {len(fila)} columnas, se esperaban {ancho}")
            desconocidas = sorted(set(fila) - set(paleta))
            if desconocidas:
                errores.append(f"fotograma {i}, fila {j}: letras sin color en la paleta: {''.join(desconocidas)}")
    if errores:
        return errores
    tira = [[paleta[c] for f in fotogramas for c in f[y]] for y in range(alto)]
    escribir_png(SALIDA / f"{ruta.stem}.png", tira)
    escribir_png(REVISION / f"{ruta.stem}_x{AMPLIACION}.png", ampliar(tira, AMPLIACION))
    print(f"  ✔ {ruta.stem}: {len(fotogramas)} fotograma(s) de {ancho}x{alto}")
    return []


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    paleta = leer_paleta()
    muestras = [c for k, c in paleta.items() if k != "."]
    if muestras:
        escribir_png(REVISION / f"paleta_x{AMPLIACION}.png", ampliar([muestras], AMPLIACION * 2))
    nombres = sys.argv[1:]
    rutas = [SPRITES / f"{n}.txt" for n in nombres] if nombres else sorted(SPRITES.glob("*.txt"))
    if not rutas:
        print("No hay sprites en estudio/sprites/.")
        return 0
    fallos = 0
    for ruta in rutas:
        if not ruta.exists():
            print(f"  ✘ {ruta.stem}: no existe")
            fallos += 1
            continue
        errores = convertir(ruta, paleta)
        if errores:
            fallos += 1
            print(f"  ✘ {ruta.stem}:")
            for e in errores[:10]:
                print(f"      {e}")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
