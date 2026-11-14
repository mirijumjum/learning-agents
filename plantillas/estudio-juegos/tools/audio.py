#!/usr/bin/env python3
"""Música y efectos de sonido escritos como texto → WAV (y MIDI). Solo la biblioteca estándar.

    python tools/audio.py musica                  # estudio/audio/musica/*.txt → godot/assets/music/*.wav
    python tools/audio.py musica --midi           # además, un .mid de cada tema
    python tools/audio.py musica --soundfont GeneralUser.sf2   # sonido de instrumentos reales con FluidSynth
    python tools/audio.py sfx                     # estudio/audio/sfx.txt → godot/assets/sfx/*.wav

Formato de un tema (estudio/audio/musica/<nombre>.txt):

    tempo: 96
    voz: cuadrada 0.30 | E5/8 G5/8 A5/4 G5/8 E5/8 | D5/2 R/4 C5/4
    voz: triangulo 0.40 | C3/4 G3/4 C3/4 G3/4 | F2/4 C3/4 G2/2
    voz: ruido 0.20 | C4/8 R/8 C4/8 R/8 C4/8 R/8 C4/8 R/8

    Nota = letra (C D E F G A B), alteración opcional (# o b), octava y /duración
    (1 redonda, 2 blanca, 4 negra, 8 corchea, 16 semicorchea; un punto final la alarga la mitad: C5/4.).
    R es silencio. Las barras | solo ayudan a leer. Ondas: cuadrada, pulso, triangulo, sierra, seno, ruido.

Formato de los efectos (estudio/audio/sfx.txt), uno por línea:

    salto:  cuadrada 300->700 0.18 0.5
    moneda: pulso    988->1319 0.12 0.4
    golpe:  ruido    400->80  0.20 0.6

    nombre: onda frecuencia_inicial->frecuencia_final duración_en_segundos volumen
"""

from __future__ import annotations

import argparse
import math
import random
import re
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
MUSICA_TXT = RAIZ / "estudio" / "audio" / "musica"
SFX_TXT = RAIZ / "estudio" / "audio" / "sfx.txt"
MUSICA_WAV = RAIZ / "godot" / "assets" / "music"
SFX_WAV = RAIZ / "godot" / "assets" / "sfx"
FRECUENCIA_MUESTREO = 22050
ONDAS = ("cuadrada", "pulso", "triangulo", "sierra", "seno", "ruido")
SEMITONOS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
NOTA = re.compile(r"^(?:(?P<letra>[A-G])(?P<alt>[#b]?)(?P<oct>-?\d)|(?P<silencio>R))/(?P<dur>\d+)(?P<punto>\.?)$")
# Instrumento General MIDI aproximado para cada onda (el ruido va al canal de percusión).
PROGRAMA_MIDI = {"cuadrada": 80, "pulso": 80, "sierra": 81, "triangulo": 38, "seno": 79}


class ErrorAudio(Exception):
    pass


def onda(tipo: str, fase: float, rng: random.Random) -> float:
    """Valor de la onda en [-1, 1] para una fase en ciclos."""
    f = fase % 1.0
    if tipo == "cuadrada":
        return 1.0 if f < 0.5 else -1.0
    if tipo == "pulso":
        return 1.0 if f < 0.25 else -1.0
    if tipo == "triangulo":
        return 4 * f - 1 if f < 0.5 else 3 - 4 * f
    if tipo == "sierra":
        return 2 * f - 1
    if tipo == "seno":
        return math.sin(2 * math.pi * f)
    return rng.uniform(-1, 1)


def escribir_wav(ruta: Path, muestras: list[float]) -> float:
    """Normaliza si hace falta (pico máximo 0.9) y guarda en 16 bits mono. Devuelve el pico."""
    pico = max((abs(m) for m in muestras), default=0.0)
    escala = 0.9 / pico if pico > 0.9 else 1.0
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(FRECUENCIA_MUESTREO)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, m * escala)) * 32767)) for m in muestras))
    return min(pico, 0.9)


# ───────────────────────────── música ─────────────────────────────


def midi_de(letra: str, alt: str, octava: int) -> int:
    return 12 * (octava + 1) + SEMITONOS[letra] + (1 if alt == "#" else -1 if alt == "b" else 0)


def leer_tema(ruta: Path) -> tuple[float, list[tuple[str, float, list[tuple[int | None, float]]]]]:
    """Devuelve (tempo, voces). Cada voz: (onda, volumen, [(nota_midi o None, pulsos)])."""
    tempo = 100.0
    voces = []
    for n, linea in enumerate(ruta.read_text(encoding="utf-8").splitlines(), 1):
        linea = linea.split("//", 1)[0].strip()
        if not linea or linea.startswith("#"):
            continue
        clave, _, valor = linea.partition(":")
        clave = clave.strip().lower()
        if clave == "tempo":
            tempo = float(valor)
        elif clave == "voz":
            cabecera, _, notas = valor.partition("|")
            partes = cabecera.split()
            if not partes or partes[0] not in ONDAS:
                raise ErrorAudio(f"{ruta.name}:{n}: onda desconocida. Usa una de: {', '.join(ONDAS)}")
            volumen = float(partes[1]) if len(partes) > 1 else 0.3
            eventos = []
            for token in notas.replace("|", " ").split():
                m = NOTA.match(token)
                if not m:
                    raise ErrorAudio(f"{ruta.name}:{n}: nota no válida «{token}» (ejemplo: C5/4, F#3/8, R/2)")
                pulsos = 4 / int(m["dur"]) * (1.5 if m["punto"] else 1)
                nota = None if m["silencio"] else midi_de(m["letra"], m["alt"], int(m["oct"]))
                eventos.append((nota, pulsos))
            voces.append((partes[0], volumen, eventos))
        else:
            raise ErrorAudio(f"{ruta.name}:{n}: línea no entendida (se esperaba «tempo:» o «voz:»)")
    if not voces:
        raise ErrorAudio(f"{ruta.name}: no tiene ninguna «voz:»")
    return tempo, voces


def sintetizar(tempo: float, voces: list) -> list[float]:
    segundos_por_pulso = 60.0 / tempo
    total = max(sum(p for _, p in ev) for _, _, ev in voces) * segundos_por_pulso
    mezcla = [0.0] * (int(total * FRECUENCIA_MUESTREO) + 1)
    rng = random.Random(1)
    for tipo, volumen, eventos in voces:
        t = 0.0
        for nota, pulsos in eventos:
            duracion = pulsos * segundos_por_pulso
            if nota is not None:
                inicio = int(t * FRECUENCIA_MUESTREO)
                n = min(int(duracion * FRECUENCIA_MUESTREO), len(mezcla) - inicio)
                hz = 440.0 * 2 ** ((nota - 69) / 12)
                ataque, caida = int(0.005 * FRECUENCIA_MUESTREO), int(min(0.04, duracion / 3) * FRECUENCIA_MUESTREO)
                ruido_corto = tipo == "ruido"
                for i in range(n):
                    if ruido_corto:  # percusión: golpe que se apaga rápido
                        env = math.exp(-i / (0.04 * FRECUENCIA_MUESTREO))
                    else:
                        env = min(1.0, i / max(1, ataque), (n - i) / max(1, caida))
                    mezcla[inicio + i] += volumen * env * onda(tipo, hz * i / FRECUENCIA_MUESTREO, rng)
            t += duracion
    return mezcla


def vlq(valor: int) -> bytes:
    """Número de longitud variable de MIDI."""
    salida = [valor & 0x7F]
    valor >>= 7
    while valor:
        salida.append(0x80 | (valor & 0x7F))
        valor >>= 7
    return bytes(reversed(salida))


def escribir_midi(ruta: Path, tempo: float, voces: list) -> None:
    ppq = 480

    def pista(eventos: bytes) -> bytes:
        eventos += b"\x00\xff\x2f\x00"  # fin de pista
        return b"MTrk" + struct.pack(">I", len(eventos)) + eventos

    microsegundos = int(60_000_000 / tempo)
    pistas = [pista(b"\x00\xff\x51\x03" + microsegundos.to_bytes(3, "big"))]
    canal_melodico = 0
    for tipo, volumen, eventos in voces:
        if tipo == "ruido":
            canal, datos = 9, b""
        else:
            canal = canal_melodico
            canal_melodico += 1 + (canal_melodico == 8)  # el canal 10 (índice 9) es percusión
            datos = b"\x00" + bytes([0xC0 | canal, PROGRAMA_MIDI[tipo]])
        velocidad = max(1, min(127, int(volumen * 300)))
        espera = 0
        for nota, pulsos in eventos:
            ticks = int(pulsos * ppq)
            if nota is None:
                espera += ticks
                continue
            nota_midi = 38 if canal == 9 else max(0, min(127, nota))  # caja en la percusión
            datos += vlq(espera) + bytes([0x90 | canal, nota_midi, velocidad])
            datos += vlq(ticks) + bytes([0x80 | canal, nota_midi, 0])
            espera = 0
        pistas.append(pista(datos))
    cabecera = b"MThd" + struct.pack(">IHHH", 6, 1, len(pistas), ppq)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(cabecera + b"".join(pistas))


def cmd_musica(args: argparse.Namespace) -> int:
    rutas = sorted(MUSICA_TXT.glob("*.txt"))
    if not rutas:
        print("No hay temas en estudio/audio/musica/.")
        return 0
    if args.soundfont and not shutil.which("fluidsynth"):
        raise ErrorAudio("Has pedido --soundfont pero fluidsynth no está en el PATH.")
    for ruta in rutas:
        tempo, voces = leer_tema(ruta)
        wav = MUSICA_WAV / f"{ruta.stem}.wav"
        if args.midi or args.soundfont:
            midi = MUSICA_WAV / f"{ruta.stem}.mid"
            escribir_midi(midi, tempo, voces)
        if args.soundfont:
            subprocess.run(["fluidsynth", "-ni", "-F", str(wav), "-r", "44100", args.soundfont, str(midi)],
                           check=True, capture_output=True)
            print(f"  ✔ {ruta.stem}: renderizado con soundfont")
            continue
        muestras = sintetizar(tempo, voces)
        pico = escribir_wav(wav, muestras)
        print(f"  ✔ {ruta.stem}: {len(muestras) / FRECUENCIA_MUESTREO:.1f} s, {len(voces)} voces, pico {pico:.2f}")
    return 0


# ───────────────────────────── efectos ─────────────────────────────

LINEA_SFX = re.compile(r"^(?P<nombre>[\w-]+)\s*:\s*(?P<onda>\w+)\s+(?P<f0>[\d.]+)\s*->\s*(?P<f1>[\d.]+)"
                       r"\s+(?P<dur>[\d.]+)\s+(?P<vol>[\d.]+)\s*$")


def cmd_sfx(args: argparse.Namespace) -> int:
    if not SFX_TXT.exists():
        print("No existe estudio/audio/sfx.txt.")
        return 0
    errores = 0
    for n, linea in enumerate(SFX_TXT.read_text(encoding="utf-8").splitlines(), 1):
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        m = LINEA_SFX.match(linea)
        if not m or m["onda"] not in ONDAS:
            print(f"  ✘ línea {n} no válida: {linea}")
            errores += 1
            continue
        duracion, volumen = float(m["dur"]), float(m["vol"])
        if not 0.01 <= duracion <= 2.0 or not 0 < volumen <= 0.9:
            print(f"  ✘ {m['nombre']}: duración entre 0.01 y 2 s y volumen entre 0 y 0.9")
            errores += 1
            continue
        f0, f1 = float(m["f0"]), float(m["f1"])
        n_muestras = int(duracion * FRECUENCIA_MUESTREO)
        rng = random.Random(n)
        fase, muestras = 0.0, []
        for i in range(n_muestras):
            avance = i / n_muestras
            fase += (f0 + (f1 - f0) * avance) / FRECUENCIA_MUESTREO
            env = min(1.0, i / (0.004 * FRECUENCIA_MUESTREO)) * (1 - avance) ** 2
            muestras.append(volumen * env * onda(m["onda"], fase, rng))
        pico = escribir_wav(SFX_WAV / f"{m['nombre']}.wav", muestras)
        print(f"  ✔ {m['nombre']}: {duracion:.2f} s, pico {pico:.2f}")
    return 1 if errores else 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="comando", required=True)
    m = sub.add_parser("musica")
    m.add_argument("--midi", action="store_true", help="guardar también un .mid")
    m.add_argument("--soundfont", help="ruta a un .sf2: renderiza con FluidSynth en vez del sintetizador")
    m.set_defaults(fn=cmd_musica)
    sub.add_parser("sfx").set_defaults(fn=cmd_sfx)
    args = p.parse_args()
    try:
        return args.fn(args)
    except ErrorAudio as e:
        print(f"✘ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
