#!/usr/bin/env python3
"""Estudio de videojuegos con agentes.

Ejecuta los agentes de estudio/agentes/, el debate de ideas, el bucle de producción de cada historia,
las simulaciones de balance y el playtest. Solo usa la biblioteca estándar de Python y habla con
cualquier servidor compatible con la API de OpenAI (por defecto, Ollama en este equipo, sin internet).

    python tools/estudio.py estado              # dónde me quedé y cuál es el siguiente paso
    python tools/estudio.py comprobar           # ¿está todo instalado?
    python tools/estudio.py debate              # ideadores A y B + juez
    python tools/estudio.py agente <nombre>     # ejecuta un agente suelto
    python tools/estudio.py bucle [H-001]       # desarrollador → lint → tester → tests → revisor
    python tools/estudio.py sim                 # simulaciones de balance + economista
    python tools/estudio.py playtest            # juega el guion, captura y lo revisa el playtester
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ESTUDIO = RAIZ / "estudio"
AGENTES = ESTUDIO / "agentes"
BACKLOG = ESTUDIO / "backlog"
INFORMES = ESTUDIO / "informes"
GODOT_DIR = RAIZ / "godot"

BASE_URL = os.environ.get("ESTUDIO_BASE_URL", "http://localhost:11434/v1").rstrip("/")
API_KEY = os.environ.get("ESTUDIO_API_KEY", "ollama")
MODELOS = {
    "normal": os.environ.get("ESTUDIO_MODELO", "qwen3:8b"),
    "rapido": os.environ.get("ESTUDIO_MODELO_RAPIDO", "qwen3:4b"),
    "vision": os.environ.get("ESTUDIO_MODELO_VISION", "qwen3-vl:8b"),
}
MAX_INTENTOS = int(os.environ.get("ESTUDIO_MAX_INTENTOS", "3"))
GODOT_BIN = os.environ.get("GODOT_BIN", "")

# Dónde puede escribir un agente. Nadie toca sus propias instrucciones ni los addons.
ZONAS_TODAS = ("estudio/", "godot/")
ZONAS_PROHIBIDAS = ("estudio/agentes/", "godot/addons/", "godot/.godot/")
MAX_CARACTERES_ARCHIVO = 20_000
MAX_CARACTERES_CODIGO = 60_000

# Tolerantes con lo que añaden los modelos: espacios, negritas, almohadillas o comillas invertidas.
MARCA_ARCHIVO = re.compile(r"^[\s*#>`]*={2,}\s*archivo\s*:\s*(.+?)\s*={2,}[\s*`]*$", re.IGNORECASE)
MARCA_FIN = re.compile(r"^[\s*#>`]*={2,}\s*fin\s*={2,}[\s*`]*$", re.IGNORECASE)
VALLA = re.compile(r"^\s*```[\w+-]*\s*$")
PENSAMIENTO = re.compile(r"<think>.*?</think>", re.DOTALL)


class ErrorEstudio(Exception):
    """Un error que se explica al usuario sin traza."""


# ───────────────────────────── utilidades ─────────────────────────────


def rel(ruta: Path) -> str:
    return ruta.resolve().relative_to(RAIZ).as_posix()


def leer(ruta: Path) -> str:
    return ruta.read_text(encoding="utf-8") if ruta.exists() else ""


def escribir(ruta: Path, texto: str, anadir: bool = False) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with ruta.open("a" if anadir else "w", encoding="utf-8", newline="\n") as f:
        f.write(texto if texto.endswith("\n") else texto + "\n")


def registrar(linea: str) -> None:
    """El archivista: deja constancia de cada paso en estudio/registro.md."""
    hora = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    escribir(ESTUDIO / "registro.md", f"- {hora} · {linea}", anadir=True)


def ahora() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def separar_frontmatter(texto: str) -> tuple[dict[str, str], str]:
    """Lee un bloque `---` inicial con líneas `clave: valor`."""
    if not texto.startswith("---"):
        return {}, texto
    partes = texto.split("\n---", 1)
    if len(partes) < 2:
        return {}, texto
    cabecera = partes[0].lstrip("-").strip()
    cuerpo = partes[1].lstrip("-").lstrip("\n")
    datos: dict[str, str] = {}
    for linea in cabecera.splitlines():
        if ":" in linea:
            clave, valor = linea.split(":", 1)
            datos[clave.strip()] = valor.strip()
    return datos, cuerpo


def unir_frontmatter(datos: dict[str, str], cuerpo: str) -> str:
    cabecera = "\n".join(f"{k}: {v}" for k, v in datos.items())
    return f"---\n{cabecera}\n---\n{cuerpo}"


def expandir(patrones: list[str]) -> list[Path]:
    rutas: list[Path] = []
    for patron in patrones:
        patron = patron.strip()
        if not patron:
            continue
        encontrados = sorted(glob.glob(str(RAIZ / patron), recursive=True))
        if encontrados:
            rutas.extend(Path(p) for p in encontrados if Path(p).is_file())
        else:
            rutas.append(RAIZ / patron)  # se mostrará como "no existe todavía"
    return list(dict.fromkeys(rutas))  # sin repetidos, en orden


def contexto_de(rutas: list[Path]) -> str:
    bloques = []
    for ruta in rutas:
        nombre = rel(ruta) if ruta.exists() else ruta.relative_to(RAIZ).as_posix()
        if not ruta.exists():
            bloques.append(f"### {nombre}\n(no existe todavía)")
            continue
        texto = leer(ruta)
        if len(texto) > MAX_CARACTERES_ARCHIVO:
            texto = texto[:MAX_CARACTERES_ARCHIVO] + "\n[… recortado …]"
        bloques.append(f"### {nombre}\n{texto}")
    return "\n\n".join(bloques)


# ───────────────────────────── modelo ─────────────────────────────


def llamar_modelo(modelo: str, sistema: str, usuario: str, imagenes: list[Path] | None = None,
                  temperatura: float = 0.7) -> str:
    contenido: object = usuario
    if imagenes:
        partes: list[dict] = [{"type": "text", "text": usuario}]
        for img in imagenes:
            datos = base64.b64encode(img.read_bytes()).decode()
            partes.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{datos}"}})
        contenido = partes
    cuerpo = {
        "model": modelo,
        "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": contenido}],
        "temperature": temperatura,
    }
    peticion = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(cuerpo).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
    )
    try:
        with urllib.request.urlopen(peticion, timeout=1800) as respuesta:
            datos = json.load(respuesta)
    except urllib.error.HTTPError as e:
        detalle = e.read().decode(errors="replace")[:500]
        raise ErrorEstudio(f"El servidor de modelos respondió {e.code}: {detalle}\n"
                           f"¿Está descargado el modelo «{modelo}»? Prueba: ollama pull {modelo}") from e
    except urllib.error.URLError as e:
        raise ErrorEstudio(f"No hay servidor de modelos en {BASE_URL} ({e.reason}).\n"
                           "Arranca Ollama (abre la app o ejecuta: ollama serve).") from e
    texto = datos["choices"][0]["message"].get("content") or ""
    return PENSAMIENTO.sub("", texto).strip()


# ───────────────────────────── agentes ─────────────────────────────


def cargar_agente(nombre: str) -> tuple[dict[str, str], str]:
    ruta = AGENTES / f"{nombre}.md"
    if not ruta.exists():
        disponibles = ", ".join(sorted(p.stem for p in AGENTES.glob("*.md")))
        raise ErrorEstudio(f"No existe el agente «{nombre}». Disponibles: {disponibles}")
    return separar_frontmatter(leer(ruta))


def ruta_permitida(texto: str, zonas: tuple[str, ...], prohibidas: tuple[str, ...] = ()) -> Path | None:
    limpia = texto.strip().strip("`").replace("\\", "/")
    while limpia.startswith("./"):
        limpia = limpia[2:]
    if not limpia or limpia.startswith("/") or ".." in limpia.split("/") or ":" in limpia:
        return None
    if not limpia.startswith(zonas) or limpia.startswith(ZONAS_PROHIBIDAS + prohibidas):
        return None
    return RAIZ / limpia


def quitar_vallas(texto: str) -> str:
    """Quita las vallas ``` que envuelven el bloque o que han quedado sueltas en sus bordes."""
    lineas = texto.strip("\n").splitlines()
    # Una cola de vallas seguidas (```, línea en blanco, ```markdown…) nunca es contenido: fuera.
    cola = 0
    while cola < len(lineas) and (VALLA.match(lineas[-1 - cola]) or not lineas[-1 - cola].strip()):
        cola += 1
    if sum(1 for linea in lineas[len(lineas) - cola:] if VALLA.match(linea)) >= 2:
        del lineas[len(lineas) - cola:]
    while lineas:
        impar = sum(1 for linea in lineas if VALLA.match(linea)) % 2 == 1
        if len(lineas) >= 2 and VALLA.match(lineas[0]) and VALLA.match(lineas[-1]):
            lineas = lineas[1:-1]
        elif impar and VALLA.match(lineas[-1]):
            lineas = lineas[:-1]
        elif impar and VALLA.match(lineas[0]):
            lineas = lineas[1:]
        else:
            break
        while lineas and not lineas[-1].strip():
            lineas.pop()
    return "\n".join(lineas)


def extraer_archivos(salida: str) -> list[tuple[str, str]]:
    """Recoge los bloques `=== archivo: ruta ===` … (hasta el siguiente bloque o `=== fin ===`)."""
    archivos: list[tuple[str, str]] = []
    actual: str | None = None
    lineas: list[str] = []
    for linea in salida.splitlines():
        marca = MARCA_ARCHIVO.match(linea)
        if marca or MARCA_FIN.match(linea):
            if actual is not None:
                archivos.append((actual, quitar_vallas("\n".join(lineas))))
            actual = marca.group(1) if marca else None
            lineas = []
        elif actual is not None:
            lineas.append(linea)
    if actual is not None:
        archivos.append((actual, quitar_vallas("\n".join(lineas))))
    return archivos


def ejecutar_agente(
    nombre: str,
    pide: str = "",
    lee: list[str] | None = None,
    escribe: str | None = None,
    anadir_titulo: str | None = None,
    zonas: tuple[str, ...] = ZONAS_TODAS,
    prohibidas: tuple[str, ...] = (),
    contexto_extra: str = "",
    imagenes: list[Path] | None = None,
) -> tuple[str, list[str]]:
    """Ejecuta un agente. Devuelve su salida y las rutas que ha escrito."""
    config, instrucciones = cargar_agente(nombre)
    modelo = MODELOS.get(config.get("modelo", "normal"), config.get("modelo", MODELOS["normal"]))
    patrones = [p for p in config.get("lee", "").split(",") if p.strip()] + (lee or [])
    mensaje = "\n\n".join(
        parte for parte in (
            "## Archivos del proyecto\n\n" + contexto_de(expandir(patrones)) if patrones else "",
            contexto_extra,
            "## Tarea\n\n" + (pide or "Haz tu trabajo según tus instrucciones."),
        ) if parte
    )
    print(f"→ {nombre} ({modelo}) trabajando…", flush=True)
    temperatura = float(config.get("temperatura", "0.7"))
    salida = llamar_modelo(modelo, instrucciones, mensaje, imagenes, temperatura)
    escribir(INFORMES / "salidas" / f"{nombre}.md", salida)  # la última respuesta completa, para depurar

    escritos: list[str] = []
    for ruta_texto, contenido in extraer_archivos(salida):
        ruta = ruta_permitida(ruta_texto, zonas, prohibidas)
        if ruta is None:
            print(f"  ⚠ ignorado (fuera de su zona): {ruta_texto}")
            continue
        escribir(ruta, contenido)
        escritos.append(rel(ruta))
    destino = escribe if escribe is not None else config.get("escribe", "")
    if not escritos and destino:
        texto = f"\n## {anadir_titulo}\n\n{salida}" if anadir_titulo else salida
        escribir(RAIZ / destino, texto, anadir=bool(anadir_titulo))
        escritos.append(destino)
    for ruta in escritos:
        print(f"  ✎ {ruta}")
    registrar(f"{nombre}: {', '.join(escritos) or 'sin archivos'}")
    return salida, escritos


# ───────────────────────────── historias ─────────────────────────────


def historias() -> list[tuple[Path, dict[str, str], str]]:
    resultado = []
    for ruta in sorted(BACKLOG.glob("H-*.md")):
        datos, cuerpo = separar_frontmatter(leer(ruta))
        resultado.append((ruta, datos, cuerpo))
    return resultado


def actualizar_historia(ruta: Path, **cambios: object) -> None:
    datos, cuerpo = separar_frontmatter(leer(ruta))
    datos.update({k.replace("_", "-"): str(v) for k, v in cambios.items()})
    escribir(ruta, unir_frontmatter(datos, cuerpo))


def siguiente_historia() -> Path | None:
    pendientes = [(int(d.get("prioridad", "99") or 99), r.name, r) for r, d, _ in historias()
                  if d.get("estado", "pendiente") in ("pendiente", "en-desarrollo")]
    return min(pendientes)[2] if pendientes else None


def archivos_de_codigo() -> list[Path]:
    rutas = []
    for patron in ("**/*.gd", "**/*.tscn", "**/*.tres", "project.godot", "playtest/*.json"):
        for p in GODOT_DIR.glob(patron):
            partes = p.relative_to(GODOT_DIR).parts
            if partes and partes[0] in ("addons", ".godot"):
                continue
            rutas.append(p)
    return sorted(set(rutas))


def contexto_codigo() -> str:
    texto = contexto_de(archivos_de_codigo())
    if len(texto) > MAX_CARACTERES_CODIGO:
        texto = texto[:MAX_CARACTERES_CODIGO] + "\n[… código recortado: el proyecto ya es grande …]"
    return "## Código actual del juego\n\n" + (texto or "(todavía no hay código)")


def firmas() -> str:
    """Solo las firmas públicas: el tester no ve la implementación (no puede copiar sus errores)."""
    patron = re.compile(r"^\s*(class_name|extends|signal|func|const|enum|@export)\b.*")
    bloques = []
    for ruta in archivos_de_codigo():
        if ruta.suffix != ".gd" or "tests" in ruta.relative_to(GODOT_DIR).parts:
            continue
        lineas = [m.group(0).rstrip(":").rstrip() for m in map(patron.match, leer(ruta).splitlines()) if m]
        if lineas:
            bloques.append(f"### {rel(ruta)}\n" + "\n".join(lineas))
    return "## Firmas del código (sin implementación)\n\n" + ("\n\n".join(bloques) or "(sin código)")


# ───────────────────────────── Godot ─────────────────────────────


def godot() -> str:
    if not GODOT_BIN or not Path(GODOT_BIN).exists():
        raise ErrorEstudio("Falta GODOT_BIN: la ruta al ejecutable *_console.exe de Godot.\n"
                           "Ejemplo (PowerShell): $env:GODOT_BIN = \"C:\\Godot\\Godot_v4.x-stable_win64_console.exe\"")
    return GODOT_BIN


def ejecutar(comando: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        r = subprocess.run(comando, cwd=RAIZ, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"Tiempo agotado ({timeout} s): {' '.join(comando)}"
    return r.returncode, (r.stdout + r.stderr).strip()


def importar_recursos() -> None:
    """Importa assets nuevos. `--import` existe en Godot 4 reciente; si no, se abre y cierra el editor."""
    codigo, _ = ejecutar([godot(), "--headless", "--path", str(GODOT_DIR), "--import"])
    if codigo != 0:
        ejecutar([godot(), "--headless", "--path", str(GODOT_DIR), "--editor", "--quit"])


def pasar_tests() -> tuple[bool, str]:
    if not (GODOT_DIR / "addons" / "gut").exists():
        raise ErrorEstudio("Falta GUT en godot/addons/gut (Sesión 1 de la guía).")
    importar_recursos()
    codigo, salida = ejecutar([godot(), "--headless", "--path", str(GODOT_DIR), "-s",
                               "res://addons/gut/gut_cmdln.gd", "-gdir=res://tests",
                               "-ginclude_subdirs", "-gexit"])
    return codigo == 0, salida[-6000:]


def pasar_lint(rutas: list[str]) -> tuple[bool, str]:
    gd = [str(RAIZ / r) for r in rutas if r.endswith(".gd")]
    if not gd:
        return True, "sin archivos .gd nuevos"
    if not shutil.which("gdlint"):
        return True, "gdlint no instalado: se omite (pip install gdtoolkit)"
    codigo, salida = ejecutar(["gdlint", *gd])
    return codigo == 0, salida[-4000:]


# ───────────────────────────── comandos ─────────────────────────────


def cmd_agente(args: argparse.Namespace) -> None:
    imagenes = [r for r in expandir(args.imagenes) if r.exists() and r.suffix.lower() == ".png"]
    if args.imagenes and not imagenes:
        raise ErrorEstudio(f"No hay PNG que coincidan con: {' '.join(args.imagenes)}")
    ejecutar_agente(args.nombre, pide=args.pide or "", lee=args.lee, escribe=args.escribe,
                    imagenes=imagenes or None)


def cmd_debate(args: argparse.Namespace) -> None:
    if not leer(ESTUDIO / "gustos.md").strip():
        raise ErrorEstudio("Primero rellena estudio/gustos.md (Sesión 2 de la guía).")
    ejecutar_agente("ideador-a")
    ejecutar_agente("ideador-b")
    for ronda in range(1, args.rondas + 1):
        ejecutar_agente("ideador-a", lee=["estudio/concepto/debate.md"], escribe="estudio/concepto/debate.md",
                        anadir_titulo=f"Réplica del ideador A (ronda {ronda})",
                        pide="Responde a las críticas del ideador B. Defiende, mejora o abandona cada idea. "
                             "No propongas ideas nuevas. Escribe en Markdown, sin bloques de archivo.")
        ejecutar_agente("ideador-b", escribe="estudio/concepto/debate.md",
                        anadir_titulo=f"Cierre del ideador B (ronda {ronda})",
                        pide="Lee la réplica y di qué ideas siguen en pie y con qué recortes. "
                             "Escribe en Markdown, sin bloques de archivo.")
    ejecutar_agente("juez")
    print("\nSiguiente: lee estudio/concepto/decision.md, elige una idea y márcala en estudio/decisiones.md")


def cmd_bucle(args: argparse.Namespace) -> None:
    godot()  # falla pronto si no está configurado
    ruta = BACKLOG / f"{args.historia}.md" if args.historia else siguiente_historia()
    if ruta is None or not ruta.exists():
        raise ErrorEstudio("No hay historias pendientes en estudio/backlog/ (Sesión 4 de la guía).")
    hid = ruta.stem
    print(f"Historia {hid}")
    datos, _ = separar_frontmatter(leer(ruta))
    intentos = int(datos.get("intentos", "0") or 0)
    comentarios = ""
    nombre_test = f"godot/tests/test_{hid.lower().replace('-', '_')}.gd"

    for _ in range(MAX_INTENTOS):
        intentos += 1
        actualizar_historia(ruta, estado="en-desarrollo", intentos=intentos)
        print(f"\nIntento {intentos}")
        pide = f"Implementa la historia {hid}. Escribe solo archivos de godot/ (no los tests)."
        if comentarios:
            pide += f"\n\nEl intento anterior falló. Corrige esto:\n\n{comentarios}"
        _, escritos = ejecutar_agente(
            "desarrollador", pide=pide, lee=[rel(ruta)], contexto_extra=contexto_codigo(),
            zonas=("godot/",), prohibidas=("godot/tests/",), escribe="")
        if not escritos:
            print("  sin archivos: la respuesta está en estudio/informes/salidas/desarrollador.md")
            comentarios = ("No escribiste ningún archivo. Cada archivo debe empezar con una línea exacta "
                           "`=== archivo: godot/scripts/nombre.gd ===` y terminar con `=== fin ===`.")
            continue

        ok, salida = pasar_lint(escritos)
        print(f"  lint: {'ok' if ok else 'falla'}")
        if not ok:
            comentarios = f"gdlint encontró problemas:\n{salida}"
            continue

        if not (RAIZ / nombre_test).exists():
            ejecutar_agente("tester", lee=[rel(ruta)], contexto_extra=firmas(), zonas=("godot/tests/",),
                            escribe="", pide=f"Escribe los tests de {hid} en {nombre_test}.")

        ok, salida = pasar_tests()
        print(f"  tests: {'ok' if ok else 'fallan'}")
        if not ok:
            comentarios = f"Los tests fallan. Salida de GUT:\n{salida}"
            continue

        revision, _ = ejecutar_agente(
            "revisor", lee=[rel(ruta), *escritos], escribe="",
            pide=f"Revisa la implementación de {hid}. Lint y tests pasan.")
        escribir(INFORMES / "revision" / f"{hid}-intento{intentos}.md", revision)
        if "VEREDICTO: CAMBIOS" in revision.upper():
            print("  revisión: pide cambios")
            comentarios = f"El revisor pide cambios:\n{revision}"
            continue

        actualizar_historia(ruta, estado="lista-para-playtest")
        registrar(f"{hid} lista para playtest tras {intentos} intento(s)")
        print(f"\n✔ {hid} lista para playtest. Haz commit: git add -A; git commit -m \"{hid}\"")
        return

    actualizar_historia(ruta, estado="bloqueada")
    escribir(INFORMES / "revision" / f"{hid}-bloqueada.md", comentarios)
    registrar(f"{hid} bloqueada tras {intentos} intentos")
    print(f"\n✘ {hid} bloqueada. Motivo en estudio/informes/revision/{hid}-bloqueada.md")


def resumir(partidas: list[dict]) -> str:
    claves = sorted({k for p in partidas for k in p if k != "semilla"})
    lineas = [f"Partidas simuladas: {len(partidas)}", "", "| métrica | media | mín | p10 | p50 | p90 | máx |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for clave in claves:
        valores = [p[clave] for p in partidas if clave in p]
        if all(isinstance(v, bool) for v in valores):
            lineas.append(f"| {clave} (% sí) | {100 * sum(valores) / len(valores):.1f} % | | | | | |")
        elif all(isinstance(v, (int, float)) for v in valores):
            orden = sorted(valores)
            pct = lambda q: orden[min(len(orden) - 1, int(q * len(orden)))]  # noqa: E731
            lineas.append(f"| {clave} | {statistics.fmean(orden):.2f} | {orden[0]} | {pct(.1)} | "
                          f"{pct(.5)} | {pct(.9)} | {orden[-1]} |")
    return "\n".join(lineas)


def cmd_sim(args: argparse.Namespace) -> None:
    salida = INFORMES / "balance" / f"sim-{ahora()}.json"
    salida.parent.mkdir(parents=True, exist_ok=True)
    importar_recursos()  # para que las clases con class_name estén registradas
    codigo, texto = ejecutar([godot(), "--headless", "--path", str(GODOT_DIR), "-s", "res://sim/simular.gd",
                              "--", f"--partidas={args.partidas}", f"--salida={salida}"], timeout=3600)
    if codigo != 0 or not salida.exists():
        raise ErrorEstudio(f"La simulación falló:\n{texto[-3000:]}")
    partidas = json.loads(leer(salida)).get("partidas", [])
    resumen = resumir(partidas)
    ruta_resumen = salida.with_suffix(".md")
    escribir(ruta_resumen, resumen)
    print(resumen)
    ejecutar_agente("economista", lee=[rel(ruta_resumen)],
                    escribe=rel(salida.with_name(salida.stem + "-informe.md")))


def cmd_playtest(args: argparse.Namespace) -> None:
    guion = RAIZ / args.guion
    if not guion.exists():
        raise ErrorEstudio(f"No existe el guion {args.guion}.")
    carpeta = INFORMES / "playtest" / ahora()
    carpeta.mkdir(parents=True, exist_ok=True)
    importar_recursos()
    codigo, texto = ejecutar([godot(), "--path", str(GODOT_DIR), "--fixed-fps", "60", "--",
                              f"--playtest-guion={guion}", f"--playtest-salida={carpeta}"], timeout=900)
    escribir(carpeta / "salida-godot.txt", texto)
    errores = [l for l in texto.splitlines() if "ERROR" in l or "SCRIPT ERROR" in l]
    capturas = sorted(carpeta.glob("captura_*.png"))
    print(f"  {len(capturas)} capturas, {len(errores)} líneas de error, código {codigo}")
    if not capturas:
        raise ErrorEstudio(f"No hay capturas. ¿Está el autoload Playtest en project.godot?\n{texto[-2000:]}")
    elegidas = capturas[:: max(1, len(capturas) // 6)][:6]
    extra = ("## Errores de Godot durante la partida\n\n" + ("\n".join(errores[:50]) or "(ninguno)")
             + "\n\n## Guion jugado\n\n" + leer(guion)
             + "\n\n## Capturas adjuntas (en orden)\n\n" + "\n".join(c.name for c in elegidas))
    ejecutar_agente("playtester", contexto_extra=extra, imagenes=elegidas,
                    escribe=rel(carpeta / "informe.md"))


DECISIONES = re.compile(r"^- \[( |x|X)\] (.+)$", re.MULTILINE)


def cmd_estado(args: argparse.Namespace) -> None:
    def hay(ruta: str) -> bool:
        return bool(leer(RAIZ / ruta).strip())

    decisiones = {texto.split("·")[0].strip(): marca.lower() == "x"
                  for marca, texto in DECISIONES.findall(leer(ESTUDIO / "decisiones.md"))}
    lista = historias()
    por_estado: dict[str, int] = {}
    for _, d, _ in lista:
        por_estado[d.get("estado", "pendiente")] = por_estado.get(d.get("estado", "pendiente"), 0) + 1

    pasos = [
        ("Gustos escritos", hay("estudio/gustos.md"), "Sesión 2: rellena estudio/gustos.md"),
        ("Debate hecho", hay("estudio/concepto/decision.md"), "Sesión 2: python tools/estudio.py debate"),
        ("D1 · idea elegida", decisiones.get("D1", False), "Sesión 2: marca D1 en estudio/decisiones.md"),
        ("GDD", hay("estudio/gdd.md"), "Sesión 3: python tools/estudio.py agente game-designer"),
        ("Guía de estilo", hay("estudio/guia-estilo.md") and hay("estudio/paleta.txt"),
         "Sesión 3: python tools/estudio.py agente director-arte"),
        ("D2 · estilo aprobado", decisiones.get("D2a", False), "Sesión 3: marca D2a en estudio/decisiones.md"),
        ("Backlog", bool(lista), "Sesión 4: python tools/estudio.py agente product-owner"),
        ("Historias terminadas", bool(lista) and not any(d.get("estado", "pendiente") in
                                                         ("pendiente", "en-desarrollo") for _, d, _ in lista),
         "Sesión 5: python tools/estudio.py bucle"),
        ("D2 · música aprobada", decisiones.get("D2b", False), "Sesión 7: marca D2b en estudio/decisiones.md"),
        ("D3 · hito jugado", decisiones.get("D3", False), "Sesión 10: juega el hito y marca D3"),
    ]
    print(f"Proyecto: {RAIZ}\n")
    siguiente = None
    for titulo, hecho, como in pasos:
        print(f"  {'✔' if hecho else '·'} {titulo}")
        if not hecho and siguiente is None:
            siguiente = como
    if lista:
        print("\n  Historias: " + ", ".join(f"{n} {e}" for e, n in sorted(por_estado.items())))
    bloqueadas = [r.stem for r, d, _ in lista if d.get("estado") == "bloqueada"]
    if bloqueadas:
        print(f"  ⚠ Bloqueadas (te toca mirarlas): {', '.join(bloqueadas)}")
    print(f"\nSiguiente paso: {siguiente or 'publicar (Sesión 11) o empezar el hito siguiente'}")


def cmd_comprobar(args: argparse.Namespace) -> None:
    fallos = 0

    def linea(ok: bool, texto: str, ayuda: str = "", opcional: bool = False) -> None:
        nonlocal fallos
        fallos += not ok and not opcional
        print(f"  {'✔' if ok else '✘'} {texto}" + (f"\n      → {ayuda}" if not ok and ayuda else ""))

    print(f"Python {sys.version.split()[0]}")
    try:
        with urllib.request.urlopen(f"{BASE_URL}/models", timeout=10) as r:
            disponibles = {m["id"] for m in json.load(r).get("data", [])}
        linea(True, f"Servidor de modelos en {BASE_URL}")
        for uso, modelo in MODELOS.items():
            linea(modelo in disponibles or f"{modelo}:latest" in disponibles,
                  f"Modelo {uso}: {modelo}", f"ollama pull {modelo}")
    except (urllib.error.URLError, OSError) as e:
        linea(False, f"Servidor de modelos en {BASE_URL} ({e})", "abre Ollama o ejecuta: ollama serve")
    if GODOT_BIN and Path(GODOT_BIN).exists():
        _, version = ejecutar([GODOT_BIN, "--version"], timeout=60)
        linea(True, f"Godot {version.splitlines()[-1] if version else '?'}")
    else:
        linea(False, "Godot (GODOT_BIN)", "define GODOT_BIN con la ruta al *_console.exe")
    linea((GODOT_DIR / "addons" / "gut").exists(), "GUT en godot/addons/gut", "Sesión 1, paso 3")
    linea(bool(shutil.which("gdlint")), "gdlint (gdtoolkit)", "pip install --no-index --find-links <mochila>/wheels gdtoolkit")
    linea(bool(shutil.which("fluidsynth")), "fluidsynth (opcional: música con soundfont)",
          "solo hace falta para --soundfont", opcional=True)
    print("\nTodo listo." if fallos == 0 else f"\n{fallos} cosa(s) por resolver.")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="estudio", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="comando", required=True)
    sub.add_parser("estado", help="dónde me quedé").set_defaults(fn=cmd_estado)
    sub.add_parser("comprobar", help="comprueba instalación y modelos").set_defaults(fn=cmd_comprobar)
    d = sub.add_parser("debate", help="ideadores + juez")
    d.add_argument("--rondas", type=int, default=1)
    d.set_defaults(fn=cmd_debate)
    a = sub.add_parser("agente", help="ejecuta un agente suelto")
    a.add_argument("nombre")
    a.add_argument("--pide", help="tarea concreta (si no, la de sus instrucciones)")
    a.add_argument("--lee", nargs="*", default=[], help="archivos extra que debe leer")
    a.add_argument("--escribe", help="archivo donde guardar la respuesta")
    a.add_argument("--imagenes", nargs="*", default=[], help="PNG que debe mirar (modelos con visión)")
    a.set_defaults(fn=cmd_agente)
    b = sub.add_parser("bucle", help="produce una historia")
    b.add_argument("historia", nargs="?")
    b.set_defaults(fn=cmd_bucle)
    s = sub.add_parser("sim", help="simulaciones de balance")
    s.add_argument("--partidas", type=int, default=500)
    s.set_defaults(fn=cmd_sim)
    t = sub.add_parser("playtest", help="juega el guion y lo revisa")
    t.add_argument("--guion", default="godot/playtest/guion.json")
    t.set_defaults(fn=cmd_playtest)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except ErrorEstudio as e:
        print(f"\n✘ {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
