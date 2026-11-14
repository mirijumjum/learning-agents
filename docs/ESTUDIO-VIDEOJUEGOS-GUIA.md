# Guía paso a paso: el estudio de videojuegos con agentes

Cómo montar y usar, sesión a sesión, el estudio descrito en [ESTUDIO-VIDEOJUEGOS.md](ESTUDIO-VIDEOJUEGOS.md):
agentes que proponen ideas, diseñan, programan en Godot, prueban, dibujan, componen, equilibran y juegan
el juego, mientras tú tomas las tres decisiones importantes.

**Funciona sin internet.** Solo necesitas conexión una vez, para preparar la "mochila" (Sesión 0), y al
final, para publicar. Todo lo demás corre en tu equipo con Ollama, Godot y Python.

La plantilla del proyecto está en [plantillas/estudio-juegos/](../plantillas/estudio-juegos/). Sus
herramientas solo usan la biblioteca estándar de Python: no hay que instalar paquetes para usarlas.

## Índice

- [Cómo usar esta guía](#cómo-usar-esta-guía)
- [Qué hay en la plantilla](#qué-hay-en-la-plantilla)
- [Sesión 0 · Preparar la mochila 🌐](#sesión-0--preparar-la-mochila-)
- [Sesión 1 · Crear el proyecto 📴](#sesión-1--crear-el-proyecto-)
- [Sesión 2 · Gustos, debate y decisión 1 📴](#sesión-2--gustos-debate-y-decisión-1-)
- [Sesión 3 · Diseño y estilo, decisión 2a 📴](#sesión-3--diseño-y-estilo-decisión-2a-)
- [Sesión 4 · Backlog 📴](#sesión-4--backlog-)
- [Sesión 5 · El bucle de producción 📴](#sesión-5--el-bucle-de-producción-)
- [Sesión 6 · Gráficos 📴](#sesión-6--gráficos-)
- [Sesión 7 · Música y efectos, decisión 2b 📴](#sesión-7--música-y-efectos-decisión-2b-)
- [Sesión 8 · Balance 📴](#sesión-8--balance-)
- [Sesión 9 · Playtest 📴](#sesión-9--playtest-)
- [Sesión 10 · Hito jugable, decisión 3 📴](#sesión-10--hito-jugable-decisión-3-)
- [Sesión 11 · Publicar 🌐](#sesión-11--publicar-)
- [Anexo A · Con internet: modelos más potentes 🌐](#anexo-a--con-internet-modelos-más-potentes-)
- [Anexo B · El mes de prueba de Microsoft 365 🌐](#anexo-b--el-mes-de-prueba-de-microsoft-365-)
- [Problemas típicos](#problemas-típicos)
- [Qué está probado y qué no](#qué-está-probado-y-qué-no)

## Cómo usar esta guía

- **🌐** necesita internet. **📴** funciona sin conexión.
- Cada sesión tiene un **objetivo**, unos **pasos** y un **✅ Funciona si…** para saber que puedes seguir.
- Las sesiones 5 a 9 se repiten: cada día que te sientes puedes hacer una historia, unos sprites o un
  playtest. No hace falta terminar una sesión de una vez.
- Los comandos son de **PowerShell** en Windows y se ejecutan desde la carpeta del juego, salvo que se
  diga otra cosa.

**Al empezar cada día:**

```powershell
cd $HOME\juegos\mi-juego
.\activar.ps1
python tools/estudio.py estado
```

`estado` te dice qué está hecho y cuál es el siguiente paso, así que no hace falta recordar dónde lo
dejaste. (`activar.ps1` lo creas en la Sesión 1).

**Al terminar cada día:**

```powershell
git add -A
git commit -m "Sesión del día: lo que hayas hecho"
```

Todo lo que producen los agentes son archivos de texto en el proyecto. Si algo sale mal, `git` te deja
volver atrás.

## Qué hay en la plantilla

| Ruta | Qué es |
| --- | --- |
| `estudio/agentes/*.md` | Las instrucciones de los 16 agentes. Cada una empieza con su configuración: qué modelo usa, qué archivos lee y dónde escribe |
| `estudio/gustos.md` | Tus gustos. Lo leen los ideadores, el juez y el game designer |
| `estudio/decisiones.md` | Tus tres decisiones, como casillas que marcas |
| `tools/estudio.py` | Ejecuta agentes, el debate, el bucle de producción, las simulaciones y el playtest |
| `tools/sprite.py` | Convierte sprites escritos como texto en PNG |
| `tools/audio.py` | Convierte música y efectos escritos como texto en WAV (y MIDI) |
| `godot/` | Proyecto Godot mínimo: escena principal, resolución de pixel art (320x180) y filtro de texturas *Nearest* |
| `godot/playtest/` | Autoload que juega un guion de entradas y hace capturas, y el guion de ejemplo |
| `godot/sim/simular.gd` | Plantilla de simulación de balance sin interfaz |
| `godot/tests/` | Tests de GUT (hay uno de ejemplo) |
| `godot/data/` | Datos del juego en JSON: el balance se cambia aquí, no en el código |

El desarrollador y el resto de agentes nunca pueden escribir en `estudio/agentes/` ni en `godot/addons/`.

## Sesión 0 · Preparar la mochila 🌐

**Objetivo:** descargar una sola vez todo lo que hace falta para trabajar sin conexión.
**Tiempo:** 1–2 horas, casi todo esperando descargas.

Crea la carpeta de la mochila:

```powershell
New-Item -ItemType Directory -Force $HOME\mochila-juegos
```

### 0.1 Godot

1. En <https://godotengine.org/download/windows/> descarga **Godot Engine** (la versión normal, no la
   .NET) para Windows de 64 bits.
2. Descomprímelo en `C:\Godot`. Dentro hay dos ejecutables:
   - `Godot_v4.X-stable_win64.exe`: el editor, para usarlo tú.
   - `Godot_v4.X-stable_win64_console.exe`: el mismo con consola. **Es el que usan las herramientas**,
     porque muestra la salida y devuelve el código de error.
3. En la misma página descarga las **Export templates** de la **misma versión** (un `.tpz` de alrededor de
   1 GB) y guárdalas en la mochila. Son necesarias para exportar el juego a Web.

### 0.2 GUT (tests para Godot)

En <https://github.com/bitwes/Gut/releases> descarga el `.zip` de la última versión de GUT compatible con
tu versión de Godot (las versiones 9.x son para Godot 4; la página de cada versión lo indica). Guárdalo
en la mochila.

### 0.3 Modelos de Ollama

Ya tienes `qwen3:8b` y `qwen3:4b`. Falta un modelo **con visión** para el director de arte y el playtester:

```powershell
ollama pull qwen3-vl:8b
```

Si ese nombre no existe en tu versión de Ollama, busca en <https://ollama.com/library> un modelo con
etiqueta *vision* (por ejemplo `qwen2.5vl:7b` o `gemma3:4b`) y apunta el nombre: lo usarás en la Sesión 1.

### 0.4 gdtoolkit (linter de GDScript)

Descarga el paquete y sus dependencias a la mochila, sin instalarlo todavía:

```powershell
python -m pip download gdtoolkit -d $HOME\mochila-juegos\wheels
```

### 0.5 Opcionales

| Qué | Para qué | Dónde |
| --- | --- | --- |
| FluidSynth para Windows + una soundfont General MIDI libre (por ejemplo GeneralUser GS) | Música con instrumentos reales en vez de chiptune | Releases de FluidSynth en GitHub; la soundfont en su web |
| Documentación de Godot sin conexión | Consultar sin internet. El editor ya trae la referencia de clases (tecla F1), que funciona sin conexión | Enlace de descarga sin conexión en <https://docs.godotengine.org> |

### 0.6 El repositorio

Asegúrate de tener la plantilla actualizada (`<ruta-del-repo>` es la carpeta donde clonaste este repositorio):

```powershell
cd <ruta-del-repo>
git pull
```

### ✅ Funciona si…

Desconecta el wifi y comprueba:

```powershell
ollama run qwen3:4b "Di hola en una palabra"
& "C:\Godot\Godot_v4.X-stable_win64_console.exe" --version
dir $HOME\mochila-juegos
```

Debe responder el modelo, Godot debe mostrar su versión y la mochila debe tener el `.tpz`, el zip de GUT y
la carpeta `wheels`.

## Sesión 1 · Crear el proyecto 📴

**Objetivo:** un proyecto del estudio listo, con Godot, GUT y el linter funcionando.
**Tiempo:** 30 minutos.

### 1.1 Copiar la plantilla

```powershell
New-Item -ItemType Directory -Force $HOME\juegos
Copy-Item -Recurse <ruta-del-repo>\plantillas\estudio-juegos $HOME\juegos\mi-juego
cd $HOME\juegos\mi-juego
git init
```

### 1.2 Entorno de Python con el linter

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index --find-links $HOME\mochila-juegos\wheels gdtoolkit
```

### 1.3 El script de cada día

Crea `activar.ps1` en la carpeta del juego con este contenido. Cambia la ruta de Godot por la tuya y, si en
la Sesión 0 elegiste otro modelo de visión, cámbialo también:

```powershell
$env:PATH = "$PSScriptRoot\.venv\Scripts;$env:PATH"
$env:GODOT_BIN = "C:\Godot\Godot_v4.X-stable_win64_console.exe"
$env:ESTUDIO_MODELO = "qwen3:8b"
$env:ESTUDIO_MODELO_RAPIDO = "qwen3:4b"
$env:ESTUDIO_MODELO_VISION = "qwen3-vl:8b"
Write-Host "Estudio listo en $PSScriptRoot"
```

Ejecútalo con `.\activar.ps1`. Pone primero en el PATH el Python del entorno (y con él `gdlint`) sin
necesidad de `Activate.ps1`, así que no hay que cambiar la política de ejecución de scripts. Si PowerShell
no te deja ejecutar `activar.ps1`, pega esas líneas directamente en la terminal.

### 1.4 GUT dentro del proyecto

Descomprime el zip de GUT de la mochila y copia su carpeta `addons\gut` en `godot\addons\gut`:

```powershell
Expand-Archive $HOME\mochila-juegos\Gut-*.zip -DestinationPath $env:TEMP\gut
$origen = Get-ChildItem -Recurse -Directory $env:TEMP\gut | Where-Object { $_.FullName -like "*\addons\gut" } | Select-Object -First 1
New-Item -ItemType Directory -Force godot\addons
Copy-Item -Recurse $origen.FullName godot\addons\gut
```

### 1.5 Abrir el proyecto una vez en el editor

1. Abre `Godot_v4.X-stable_win64.exe`, pulsa **Importar** y elige `godot\project.godot`.
2. Si avisa de que el proyecto es de otra versión, acepta convertirlo.
3. **Proyecto → Configuración del proyecto → Plugins**: activa **Gut**.
4. **Editor → Gestionar plantillas de exportación → Instalar desde archivo**: elige el `.tpz` de la mochila.
5. Cierra el editor.

### 1.6 Comprobar

```powershell
& $env:GODOT_BIN --headless --path godot -s res://addons/gut/gut_cmdln.gd -gdir=res://tests -gexit
python tools/estudio.py comprobar
git add -A
git commit -m "Proyecto del estudio"
```

### ✅ Funciona si…

GUT informa de que `test_gut_funciona` ha pasado y `comprobar` muestra ✔ en servidor de modelos, los tres
modelos, Godot, GUT y gdlint (fluidsynth es opcional).

## Sesión 2 · Gustos, debate y decisión 1 📴

**Objetivo:** que el estudio proponga ideas, las debata y te presente dos finalistas.
**Tiempo:** 45 minutos, la mayor parte esperando al modelo.

1. Edita `estudio/gustos.md`. Cuanto más concreto (qué disfrutas, qué no soportas, juegos que te han
   gustado y por qué), mejores ideas.
2. Lanza el debate:

   ```powershell
   python tools/estudio.py debate
   ```

   Se ejecutan en orden el ideador A (tres ideas), el ideador B (críticas), una réplica de cada uno y el
   juez. Con `qwen3:8b` cada agente tarda varios minutos. Para ir más rápido mientras pruebas, usa el
   modelo pequeño: `$env:ESTUDIO_MODELO = "qwen3:4b"`.
3. Lee `estudio/concepto/ideas.md`, `debate.md` y sobre todo `decision.md`.
4. ¿No te convence ninguna? Puedes repetir el debate, pedir más rondas (`debate --rondas 2`) o afinar
   `gustos.md`. También puedes pedirle algo concreto a un agente:

   ```powershell
   python tools/estudio.py agente ideador-a --pide "Tres ideas más, todas de gestión con toque roguelite"
   ```

5. **Decisión 1:** en `estudio/decisiones.md`, marca `- [x] D1 · Idea elegida: <título>`. Si quieres, añade
   cambios a la idea en "Otras decisiones y notas": el game designer las leerá.

### ✅ Funciona si…

`python tools/estudio.py estado` muestra ✔ en "Debate hecho" y "D1 · idea elegida".

## Sesión 3 · Diseño y estilo, decisión 2a 📴

**Objetivo:** el documento de diseño (GDD) y la guía de estilo visual.
**Tiempo:** 1 hora.

```powershell
python tools/estudio.py agente game-designer
```

Lee `estudio/gdd.md` y **edítalo sin miedo**: tú mandas. Fíjate sobre todo en tres secciones, porque el
resto del estudio depende de ellas:

- **Controles**: los nombres de las acciones los usan el código y el playtest.
- **Arquitectura**: la lógica tiene que ir en clases puras para poder probarla y simularla.
- **Contenido del primer hito**: si es demasiado, recórtalo ahora.

Después, el estilo visual:

```powershell
python tools/estudio.py agente director-arte
python tools/sprite.py
```

`sprite.py` todavía no tiene sprites, pero crea `estudio/informes/arte/paleta_x8.png` con las muestras de
color. Ábrelo y lee `estudio/guia-estilo.md`.

**Decisión 2a:** si te gustan el estilo y la paleta, marca `D2a` en `estudio/decisiones.md`. Si no, edita
la guía o repite el agente con `--pide "Más cálido y con menos colores"`.

### ✅ Funciona si…

Existen `estudio/gdd.md`, `estudio/guia-estilo.md` y `estudio/paleta.txt`, y `estado` muestra ✔ en D2a.

## Sesión 4 · Backlog 📴

**Objetivo:** dividir el primer hito en historias pequeñas.
**Tiempo:** 30 minutos.

```powershell
python tools/estudio.py agente product-owner
dir estudio\backlog
```

Cada historia es un archivo `H-001.md`, `H-002.md`… con su estado, su prioridad y sus criterios de
aceptación. Revísalas:

- ¿Los criterios tienen números concretos que un test pueda comprobar?
- ¿La H-001 prepara la base (escena principal, acciones de entrada, clase de estado)?
- ¿Alguna es demasiado grande? Divídela a mano o pídeselo al agente:
  `agente product-owner --pide "Divide la H-004 en dos"`.

### ✅ Funciona si…

`estado` muestra ✔ en "Backlog" y el recuento de historias pendientes.

## Sesión 5 · El bucle de producción 📴

**Objetivo:** que una historia pase sola de "pendiente" a "lista para playtest".
**Tiempo:** de 15 a 45 minutos por historia. Es la sesión que más se repite.

```powershell
python tools/estudio.py bucle
```

Sin argumentos coge la siguiente historia por prioridad. También puedes elegir una: `bucle H-003`.

Esto es lo que hace el bucle, hasta 3 intentos (`$env:ESTUDIO_MAX_INTENTOS` para cambiarlo):

```
desarrollador ──▶ gdlint ──▶ tester (solo la primera vez) ──▶ tests GUT ──▶ revisor
      ▲              │                                            │             │
      └──────────────┴──────────────── con el motivo del fallo ───┴─────────────┘
```

1. El **desarrollador** lee la historia, el GDD y el código actual, y escribe archivos en `godot/`. No puede
   tocar los tests.
2. **gdlint** revisa el estilo del GDScript (reglas fijas, sin modelo).
3. El **tester** escribe `godot/tests/test_h_00X.gd` a partir de los criterios. Solo ve las firmas de las
   clases, no la implementación, para no copiar sus errores.
4. **GUT** ejecuta todos los tests sin abrir ventana.
5. El **revisor** comprueba lo que los tests no ven y responde `VEREDICTO: OK` o `VEREDICTO: CAMBIOS`.

Cuando termina, la historia queda en uno de estos estados:

- **`lista-para-playtest`**: ábrela en el editor de Godot y pulsa F5 para verla. Haz commit.
- **`bloqueada`**: el motivo está en `estudio/informes/revision/H-00X-bloqueada.md`. Puedes hacer tres
  cosas: aclarar la historia (criterios ambiguos, demasiado grande), arreglar tú el código o el test, o
  probar con un modelo más potente (Anexo A). Para reintentarla, pon `estado: pendiente` e `intentos: 0`
  en su cabecera.

Haz **un commit por historia**: si la siguiente estropea algo, puedes volver atrás.

### ✅ Funciona si…

La historia acaba en `lista-para-playtest` y `git status` muestra los archivos nuevos en `godot/`.

## Sesión 6 · Gráficos 📴

**Objetivo:** los sprites del primer hito, revisados por el director de arte.
**Tiempo:** 1 hora.

```powershell
python tools/estudio.py agente grafista
python tools/sprite.py
```

El grafista dibuja cada sprite como una rejilla de letras en `estudio/sprites/<nombre>.txt`, y `sprite.py`
los convierte en:

- `godot/assets/sprites/<nombre>.png`: tamaño real; los fotogramas en tira horizontal.
- `estudio/informes/arte/<nombre>_x8.png`: ampliado, para revisarlo.

Si `sprite.py` encuentra errores (filas de distinto ancho, letras que no están en la paleta), díselo al
grafista:

```powershell
python tools/estudio.py agente grafista --pide "Corrige estos errores: <pega la salida de sprite.py>"
```

Revisión del director de arte, con el modelo de visión:

```powershell
python tools/estudio.py agente director-arte-revision --imagenes "estudio/informes/arte/*_x8.png" --escribe estudio/informes/arte/revision.md
python tools/estudio.py agente grafista --lee estudio/informes/arte/revision.md --pide "Rehaz los sprites marcados REHACER"
python tools/sprite.py
```

Mira tú también las imágenes ampliadas. También puedes retocar los `.txt` a mano: son una rejilla de letras.

En Godot, usa los PNG en un `Sprite2D` con `hframes` igual al número de fotogramas. Para que el
desarrollador los integre, pídele al product owner una historia nueva:
`agente product-owner --pide "Historia para usar los sprites de godot/assets/sprites en las escenas"`.

### ✅ Funciona si…

`sprite.py` termina sin errores y los sprites se reconocen en las imágenes ampliadas.

## Sesión 7 · Música y efectos, decisión 2b 📴

**Objetivo:** un tema musical y los efectos del primer hito.
**Tiempo:** 45 minutos.

```powershell
python tools/estudio.py agente compositor
python tools/audio.py musica --midi
python tools/audio.py sfx
```

Salen en `godot/assets/music/` y `godot/assets/sfx/`. Escúchalos con doble clic. Aquí juzgas tú: un modelo
no sabe si algo suena bien.

- ¿Error de formato o voces de distinta duración? Pídele al compositor que lo corrija pegando la salida.
- ¿No te gusta? `agente compositor --pide "Más lento, en modo mayor y con menos percusión"`.
- ¿Quieres instrumentos reales en vez de chiptune? Si instalaste FluidSynth y la soundfont:
  `python tools/audio.py musica --soundfont $HOME\mochila-juegos\GeneralUser.sf2`.
- El `.mid` se puede abrir y retocar en cualquier editor MIDI.

En Godot, selecciona el WAV de la música y, en el panel **Importar**, pon **Loop Mode: Forward** y pulsa
**Reimportar** para que suene en bucle.

**Decisión 2b:** cuando te guste, marca `D2b` en `estudio/decisiones.md`.

### ✅ Funciona si…

Los WAV suenan, la música repite sin cortes bruscos y `estado` muestra ✔ en D2b.

## Sesión 8 · Balance 📴

**Objetivo:** comprobar con cientos de partidas simuladas que la economía del juego tiene sentido.
**Tiempo:** 1 hora la primera vez, minutos las siguientes.

La plantilla `godot/sim/simular.gd` trae una partida de ejemplo que no tiene nada que ver con tu juego.
Primero hay que sustituirla por la lógica real con un bot sencillo:

```powershell
python tools/estudio.py agente desarrollador --lee godot/sim/simular.gd --pide "Sustituye jugar_partida() en godot/sim/simular.gd: usa las clases del juego y un bot sencillo que juegue una partida completa, y devuelve las métricas de la sección Métricas de balance del GDD"
```

Después, simula:

```powershell
python tools/estudio.py sim --partidas 500
```

Godot juega las partidas sin ventana, `estudio.py` resume las métricas (media, mínimo, percentiles,
porcentaje de victorias) y el **economista** escribe un informe en `estudio/informes/balance/` con hasta
cinco cambios concretos.

Para aplicarlos:

```powershell
python tools/estudio.py agente disenador-contenido
python tools/estudio.py sim --partidas 500
```

El diseñador de contenido lee el último informe y cambia los JSON de `godot/data/`. Vuelve a simular para
comprobar que el cambio ha funcionado.

### ✅ Funciona si…

Las métricas quedan dentro de los rangos sanos del GDD.

## Sesión 9 · Playtest 📴

**Objetivo:** que un agente "juegue" y te diga qué ve y qué falla.
**Tiempo:** 20 minutos.

1. Revisa `godot/playtest/guion.json`. Es una lista de acciones que se pulsan y se sueltan en fotogramas
   concretos (60 fotogramas = 1 segundo). Usa las acciones del GDD. El desarrollador lo actualiza cuando
   una historia añade controles.
2. Lanza el playtest:

   ```powershell
   python tools/estudio.py playtest
   ```

   Se abre la ventana del juego unos segundos (no la toques), el autoload `Playtest` pulsa las acciones
   del guion y guarda capturas. Después, el **playtester** (modelo de visión) recibe las capturas, los
   errores que imprimió Godot y el guion, y escribe `estudio/informes/playtest/<fecha>/informe.md`.
3. Convierte los bugs en historias:

   ```powershell
   python tools/estudio.py agente product-owner --lee "estudio/informes/playtest/*/informe.md" --pide "Crea historias para los bugs del último informe de playtest"
   ```

Si quieres un vídeo de la partida, el modo Movie Maker de Godot graba fotograma a fotograma:

```powershell
& $env:GODOT_BIN --path godot --write-movie partida.avi -- --playtest-guion=$PWD\godot\playtest\guion.json --playtest-salida=$PWD\build\capturas
```

### ✅ Funciona si…

Hay capturas en la carpeta del informe y el informe describe lo que pasa en ellas.

## Sesión 10 · Hito jugable, decisión 3 📴

**Objetivo:** una versión Web del juego que puedas jugar en el navegador.
**Tiempo:** 30 minutos.

1. En el editor: **Proyecto → Exportar → Añadir… → Web**.
2. Desactiva **Thread Support** (desde Godot 4.3 se puede exportar sin hilos, lo que evita problemas en
   navegadores e itch.io).
3. **Exportar proyecto** a `build\web\index.html` (crea la carpeta si no existe).
4. Pruébalo en local, sin internet:

   ```powershell
   python -m http.server 8000 -d build\web
   ```

   y abre <http://localhost:8000>.
5. **Juégalo tú.** Esta es la decisión importante: ¿apetece una partida más?
6. **Decisión 3:** si está listo para enseñarlo, marca `D3` en `estudio/decisiones.md` y etiqueta la versión:

   ```powershell
   git tag hito-1
   ```

   Si no, apunta qué falta en "Otras decisiones y notas" y pídele historias al product owner.

### ✅ Funciona si…

El juego carga en el navegador y `estado` muestra ✔ en D3.

## Sesión 11 · Publicar 🌐

**Objetivo:** el juego en itch.io.
**Tiempo:** 30 minutos.

1. 📴 Puedes preparar el texto antes, sin conexión:

   ```powershell
   python tools/estudio.py agente publicista
   ```

   Queda en `estudio/publicacion/itch.md`, en español y en inglés.
2. 🌐 Comprime `build\web` en un zip:
   `Compress-Archive build\web\* build\mi-juego-web.zip -Force`.
3. 🌐 En itch.io: **Upload new project**, tipo **HTML**, sube el zip y marca *This file will be played in
   the browser*. Pon de tamaño de ventana 1280x720 y usa capturas del playtest (`estudio\informes\playtest\`).
4. Revisa la página antes de hacerla pública. Publicarla es decisión tuya.

## Anexo A · Con internet: modelos más potentes 🌐

`estudio.py` funciona con cualquier servidor compatible con la API de OpenAI. Con conexión, puedes
apuntar algunos agentes a un modelo más potente (por ejemplo, para el desarrollador cuando una historia se
bloquea) y volver a Ollama cuando quieras:

```powershell
$env:ESTUDIO_BASE_URL = "https://<tu-proveedor>/v1"
$env:ESTUDIO_API_KEY = "<tu clave>"
$env:ESTUDIO_MODELO = "<nombre del modelo>"
python tools/estudio.py bucle H-004
Remove-Item Env:ESTUDIO_BASE_URL, Env:ESTUDIO_API_KEY; .\activar.ps1   # volver a local
```

No guardes la clave en `activar.ps1` ni en ningún archivo del repositorio.

## Anexo B · El mes de prueba de Microsoft 365 🌐

Durante la prueba, puedes llevar los agentes de "oficina" (ideadores, juez, game designer, product owner,
publicista) a Teams y Copilot Studio. **Los archivos del proyecto siguen siendo la fuente de verdad**: lo
que hagas en Microsoft 365 se copia de vuelta a `estudio/`, y así todo sigue funcionando sin conexión
cuando acabe la prueba.

Requisito: el tenant de prueba de [FIN-DE-SEMANA-M365.md](FIN-DE-SEMANA-M365.md#viernes-cuentas-e-instalación)
("Viernes: cuentas e instalación").

1. **Teams.** Crea un equipo "Estudio" con los canales *Concepto*, *Producción* y *Decisiones*.
2. **Copilot Studio.** Crea un agente por cada archivo de `estudio/agentes/` que quieras llevar:
   - **Instrucciones:** el texto del `.md` sin la cabecera `---`.
   - **Conocimiento:** sube `gustos.md` y, más adelante, `gdd.md` (o una carpeta de SharePoint con ellos).
   - Crea un agente principal "Productor" que tenga los demás como agentes conectados y publícalo en
     Teams. Así puedes lanzar el debate desde el canal *Concepto*.
3. **Planner (opcional).** Un plan "Backlog" para ver las historias en tablero. No se sincroniza solo con
   `estudio/backlog/`: úsalo para visualizar o planificar, y mantén los archivos como referencia.
4. **De vuelta a local.** Copia lo que produzcan los agentes de Teams (ideas, decisión, GDD) en sus
   archivos de `estudio/` y haz commit.
5. **Créditos.** Los agentes que se llaman entre sí pueden gastar Copilot Credits (ver
   [M365-COPILOT-APRENDER.md](M365-COPILOT-APRENDER.md#7-costes-y-trampas)). El bucle de producción
   (Sesiones 5 a 9) sigue siempre en local.

Conectar Copilot Studio con el bucle local por MCP, como hace `devassist-copilot`, queda como siguiente
paso: no está implementado para el estudio.

## Problemas típicos

| Síntoma | Causa probable | Qué hacer |
| --- | --- | --- |
| `No hay servidor de modelos en http://localhost:11434/v1` | Ollama no está arrancado | Abre la app de Ollama o ejecuta `ollama serve` |
| `El servidor de modelos respondió 404` | El modelo no está descargado | `ollama pull <modelo>` (con internet) o cambia `ESTUDIO_MODELO` por uno que tengas |
| Un agente tarda muchísimo | Modelo grande para tu equipo | Usa `qwen3:4b` para pruebas; cierra programas que usen la GPU |
| `⚠ ignorado (fuera de su zona)` | El agente intentó escribir donde no debe | Es la protección funcionando. Si se repite, revisa la tarea que le pides |
| El agente no escribe ningún archivo | No respetó el formato `=== archivo: … ===` | Mira su respuesta completa en `estudio/informes/salidas/<agente>.md`. Repite; con `qwen3:8b` falla menos que con `qwen3:4b` |
| GUT no encuentra una clase (`class_name`) | El proyecto no está importado | Abre el proyecto una vez en el editor, o `& $env:GODOT_BIN --headless --path godot --import` |
| Godot no muestra nada en la terminal | Estás usando el ejecutable sin `_console` | `GODOT_BIN` tiene que apuntar al `*_console.exe` |
| El playtest no genera capturas | Falta el autoload o el guion no es JSON válido | Comprueba `[autoload]` en `godot/project.godot` y el guion |
| Los sprites se ven borrosos | Filtro de texturas | La plantilla trae *Nearest*; si lo cambiaste, vuelve a ponerlo en Configuración del proyecto → Renderizado → Texturas |
| PowerShell no deja ejecutar `activar.ps1` | Política de ejecución de scripts | Pega sus líneas directamente en la terminal |
| Una historia siempre se bloquea | Historia grande o ambigua | Divídela, añade números a los criterios o usa un modelo más potente (Anexo A) |

## Qué está probado y qué no

Probado el 14/11/2026 en Windows 11 con Python 3.11 y Ollama (`qwen3:4b`):

- `sprite.py`: paleta, sprites de varios fotogramas, PNG válidos y detección de errores (filas de distinto
  ancho, letras fuera de la paleta).
- `audio.py`: música de tres voces a WAV y MIDI, efectos, y rechazo de valores fuera de rango.
- `estudio.py estado` y `comprobar`.
- `estudio.py agente ideador-a`: un agente real, unos 90 segundos con `qwen3:4b`.
- `estudio.py agente product-owner`: cinco historias en archivos separados a partir de un GDD de prueba.
- `estudio.py bucle` con `qwen3:8b`, `gdlint` real y un Godot simulado que siempre aprueba los tests
  (unos 4 minutos). El desarrollador no escribió archivos en el primer intento y sí en el segundo; el
  tester escribió sus tests; el revisor pidió cambios con razón (el código usaba una API que no existe en
  Godot 4) y la historia quedó bloqueada al agotar los intentos de la prueba. Es el comportamiento
  esperado: con un modelo local pequeño, cuenta con varios intentos por historia y con revisar a mano
  algunas bloqueadas.
- Las plantillas GDScript (`playtest.gd`, `simular.gd`, `test_ejemplo.gd`) pasan `gdlint` y `gdformat`.

**Sin probar** (no había Godot instalado): todo lo que ejecuta Godot (`--import`, GUT por línea de
comandos, `playtest.gd`, `simular.gd`, exportación Web), los modelos con visión, FluidSynth y el Anexo B.
Si algo de eso falla, la causa más probable es una diferencia de versión de Godot o de GUT: la salida de
error de `estudio.py` incluye la de Godot.
