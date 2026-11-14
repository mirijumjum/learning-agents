# Estudio de videojuegos con agentes

Diseño de un equipo de agentes que lleva un videojuego desde la idea hasta su publicación en itch.io, con
**Godot** como motor. El objetivo es que el estudio funcione casi solo y que la persona que lo dirige
intervenga solo en tres decisiones (las más divertidas).

Es un diseño, no una implementación. Sirve como base para probarlo durante el mes de prueba de
Microsoft 365 Copilot ([M365-COPILOT-APRENDER.md](M365-COPILOT-APRENDER.md),
[FIN-DE-SEMANA-M365.md](FIN-DE-SEMANA-M365.md)) y para la parte local con Ollama ([DEVASSIST.md](DEVASSIST.md)).

## Índice

- [1. La idea que lo ordena todo](#1-la-idea-que-lo-ordena-todo)
- [2. El flujo](#2-el-flujo)
- [3. Los agentes](#3-los-agentes)
- [4. Dónde decide la persona](#4-dónde-decide-la-persona)
- [5. Godot para agentes](#5-godot-para-agentes)
- [6. Géneros y lo que piden](#6-géneros-y-lo-que-piden)
- [7. La rúbrica del juez](#7-la-rúbrica-del-juez)
- [8. Estructura de un proyecto](#8-estructura-de-un-proyecto)
- [9. Qué corre en Microsoft 365 y qué en local](#9-qué-corre-en-microsoft-365-y-qué-en-local)
- [10. Plan para el mes de prueba](#10-plan-para-el-mes-de-prueba)
- [11. Costes y trampas](#11-costes-y-trampas)
- [12. Qué no está verificado](#12-qué-no-está-verificado)

## 1. La idea que lo ordena todo

Para que el estudio sea autosuficiente no importa tanto el número de agentes como que **cada agente que
produce algo tenga otro que lo compruebe**. Sin esa comprobación los errores se acumulan y la persona
acaba revisándolo todo.

De ahí salen tres reglas:

1. **Cada productor tiene su verificador.** Desarrollador → tester y revisor. Gráficos → director de arte.
   Economía del juego → simulaciones.
2. **Lo que se puede hacer sin modelo, se hace sin modelo.** El orquestador, el linter, la compilación y el
   despliegue son scripts normales: más fiables, más rápidos y no gastan créditos. Es el mismo principio
   que el `quality_gate` de agent-lab.
3. **El juego tiene que poder probarse sin una persona delante.** Por eso el motor y el género se eligen
   pensando en si un bot puede jugarlo y medirlo (secciones 5 y 6).

## 2. El flujo

```
 CONCEPTO                PREPRODUCCIÓN                 PRODUCCIÓN (bucle por historia)          LANZAMIENTO
 ─────────               ──────────────                ───────────────────────────────          ───────────
 Ideador A ─┐            Game Designer ── GDD          Product Owner ── siguiente historia
            ├─ debate    Director de arte ── guía       │
 Ideador B ─┘   │        Product Owner ── backlog       ▼
                ▼        Diseñador de contenido         Desarrollador ─▶ Revisor ─▶ Tester ─┐
             Juez                                       Gráficos ─▶ Director de arte        │
                │                                       Compositor / Efectos                 │
                ▼                                       Diseñador de contenido ─▶ Economista │
          [DECISIÓN 1]          [DECISIÓN 2]                     │                           │
          elegir idea           aprobar estilo                   ▼                           ▼
                                y música                    Playtester ◀── build ◀── integración
                                                                 │
                                                       ¿falla? vuelve a la historia (máx. N intentos)
                                                                 │
                                                           [DECISIÓN 3] ──▶ Publicista ──▶ itch.io
                                                           jugar el hito
```

Cada historia pasa por estos estados, y quien la mueve es el orquestador (código, no modelo):

```
pendiente → en desarrollo → revisión → test → playtest → hecha
                 ▲             │        │        │
                 └─────────────┴────────┴────────┘   (con el motivo del fallo, hasta N intentos;
                                                       después se marca "bloqueada" para la persona)
```

## 3. Los agentes

### Concepto

| Agente | Qué hace | Entrega |
| --- | --- | --- |
| **Ideador A, visionario** | Propone conceptos originales: gancho, mecánica central, por qué es divertido | `concepto/ideas.md` |
| **Ideador B, pragmático** | Lleva la contraria: recorta el alcance y pregunta si se puede hacer con gráficos generados, si un bot puede probarlo y si cabe en el tiempo | Réplicas en `concepto/debate.md` |
| **Juez / Productor** | Puntúa cada idea con la rúbrica (sección 7) y presenta las dos mejores | `concepto/decision.md` |

Los dos ideadores necesitan personalidades opuestas y explícitas en sus instrucciones. Si no, los modelos
tienden a darse la razón. El debate tiene un número fijo de rondas (por ejemplo, tres ideas por ideador y
dos rondas de réplicas) para que no se alargue sin fin.

### Preproducción

| Agente | Qué hace | Entrega |
| --- | --- | --- |
| **Game Designer** | Documento de diseño: bucle de juego, controles, reglas, cómo se gana y cómo se pierde, alcance del primer hito | `gdd.md` |
| **Director de arte** | Guía de estilo: paleta, resolución, tamaño de sprites, estilo de animación y un prompt base que reutiliza el agente de gráficos | `guia-estilo.md`, `paleta.png` |
| **Product Owner** | Divide el GDD en épicas e historias con criterios de aceptación comprobables, prioriza y mantiene el backlog | Backlog (Planner o `backlog/`) |
| **Diseñador de contenido** | Crea objetos, enemigos, recetas, eventos o niveles como datos (recursos `.tres` o JSON), nunca como código | `godot/data/` |

El director de arte es imprescindible: sin una guía de estilo común, cada imagen generada sale de un
estilo distinto.

### Producción

| Productor | Verificador | Cómo se comprueba |
| --- | --- | --- |
| **Desarrollador** (GDScript, escenas `.tscn`) | **Revisor de código** | Primero `gdlint`/`gdformat` (reglas fijas), luego el modelo revisa diseño, legibilidad y si cumple la historia |
| | **Tester** | Escribe y ejecuta tests con gdUnit4 o GUT sin interfaz. Escribe los tests a partir de los criterios de aceptación, no del código, para no copiar los errores del desarrollador |
| **Gráficos** | **Director de arte** | Un modelo con visión compara la imagen con la guía: paleta, tamaño, legibilidad a la escala del juego |
| **Compositor** (música) | **La persona** | Un modelo no sabe juzgar bien un audio. Aquí decide la persona (decisión 2) |
| **Efectos de sonido** | Reglas fijas | Duración, volumen máximo, sin silencio al principio. Son comprobaciones automáticas |
| **Diseñador de contenido** | **Economista / Balanceador** | Simula miles de partidas sin interfaz y comprueba que nada se dispara ni se atasca (curvas de recursos, duración de las runs, tasa de victoria) |
| Todo junto | **Playtester** | Juega el build con pulsaciones automáticas, graba o captura la partida y la revisa con visión: bugs, cosas que no se entienden, partes aburridas |

### Lanzamiento

| Agente | Qué hace |
| --- | --- |
| **Publicista** | Página de itch.io, descripción, etiquetas, capturas y GIF a partir de las grabaciones del playtester |

### Transversales (código, no modelos)

| Pieza | Qué hace |
| --- | --- |
| **Orquestador** | Mueve las historias entre estados, llama al agente que toca y corta tras N intentos |
| **Integración y build** | Junta código y recursos, importa los assets, exporta a Web y lo sube a itch.io |
| **Archivista** | Guarda decisiones (`decisiones.md`) y changelog para que ningún agente olvide lo ya decidido. Puede ser un agente pequeño o un simple registro que el orquestador va escribiendo |

### Solo para multijugador

| Agente | Qué hace |
| --- | --- |
| **Ingeniero de red** | Multijugador de alto nivel de Godot (`MultiplayerSpawner`, `MultiplayerSynchronizer`, `ENetMultiplayerPeer`), sincronización y autoridad. Solo entra en el friend slop |

En total son unos 16 agentes con modelo más las piezas de código. No hace falta tenerlos todos desde el
primer día (sección 10).

## 4. Dónde decide la persona

1. **Elegir entre las dos ideas finalistas** del juez.
2. **Aprobar la guía de estilo y la música.**
3. **Jugar cada hito** y dar el visto bueno antes de publicar.

Además, el orquestador le pasa cualquier historia que siga fallando tras N intentos. El resto va solo.

## 5. Godot para agentes

Godot encaja bien porque las escenas (`.tscn`) y los recursos (`.tres`) son texto, GDScript es fácil de
generar y el motor se puede ejecutar sin interfaz.

| Necesidad | Herramienta |
| --- | --- |
| Ejecutar sin ventana | `godot --headless` (usa controladores de vídeo y audio vacíos) |
| Tests | gdUnit4 o GUT, los dos con ejecutor desde la línea de comandos |
| Linter y formato | gdtoolkit: `gdlint` y `gdformat` |
| Importar assets nuevos | `godot --headless --import` (ver sección 12) |
| Simulaciones de balance | Un script lanzado con `godot --headless --script sim/xxx.gd` que juega con bots y escribe métricas en CSV o JSON |
| Playtest con pulsaciones | Un script que inyecta entradas con `Input.parse_input_event()` y guarda capturas con `get_viewport().get_texture().get_image().save_png()` |
| Grabar la partida | Modo Movie Maker: `--write-movie partida.avi` graba fotograma a fotograma, sin perder fotogramas aunque el equipo vaya lento |
| Publicar | Exportación a Web. Desde Godot 4.3 se puede exportar sin hilos, lo que evita los problemas de `SharedArrayBuffer` en navegadores e itch.io |
| Música | El compositor escribe MIDI. Se convierte en local con FluidSynth y una soundfont (`fluidsynth -ni fuente.sf2 tema.mid -F tema.wav`) y luego a OGG |
| Efectos de sonido | Generador procedural tipo sfxr: parámetros que el agente elige y un script que produce el WAV |

Una trampa: con `--headless` no se renderiza nada, así que las capturas y el Movie Maker necesitan una
ventana real. En Windows basta con lanzarlo normal (puede ir minimizado). Los tests y las simulaciones sí
van sin ventana.

## 6. Géneros y lo que piden

| Género | Qué tal lo llevan los agentes | Qué añade al estudio |
| --- | --- | --- |
| **Gestión de recursos** | ✅ Muy bien: la economía son números y se puede simular miles de veces | El economista es el verificador principal |
| **Roguelite** | ✅ Muy bien: el contenido son datos y un bot puede jugar runs | Diseñador de contenido + economista para las sinergias y la curva de dificultad |
| **Cozy** | 🟡 La mecánica es sencilla, pero el juego depende de cómo se siente | Más peso del director de arte y el compositor, y más decisiones de la persona: un modelo no sabe si algo es acogedor |
| **Friend slop** (cooperativo caótico) | 🔴 El más difícil: red, física, varios clientes a la vez, y la gracia solo se ve jugando con amigos | Ingeniero de red. El playtester lanza 2–4 instancias con bots, que prueban que funciona pero no que sea divertido |

Orden recomendado de proyectos:

1. **Gestión, o gestión + roguelite.** Es lo más fácil de verificar y sirve para probar todo el flujo.
   Por ejemplo: una caravana en la que cada run es un viaje entre pueblos administrando comida, agua y
   mercancía, con eventos aleatorios y progreso que desbloquea rutas.
2. **Cozy + gestión**, cuando el director de arte y el compositor estén afinados.
3. **Friend slop**, al final, cuando todo lo demás funcione.

## 7. La rúbrica del juez

Cada idea se puntúa de 1 a 5 en cada criterio. El peso refleja qué importa más para este estudio.

| Criterio | Peso | Pregunta |
| --- | --- | --- |
| **Encaja con los gustos** | 2 | ¿Es cozy, gestión, roguelite o friend slop? ¿Combina varios? |
| **Un bot puede comprobarlo** | 3 | ¿Puede un agente saber si funciona y si está equilibrado sin que juegue la persona? |
| **Alcance** | 2 | ¿Cabe el primer hito en el tiempo disponible con una sola mecánica central? |
| **Viable con assets generados** | 2 | ¿Funciona con pixel art o un estilo sencillo y coherente? ¿Evita animaciones complejas? |
| **Diversión** | 2 | ¿Tiene un bucle que apetece repetir? |
| **Originalidad** | 1 | ¿Tiene un gancho propio? No vale una copia de un juego existente con otro nombre |

El juez entrega la tabla de puntuaciones, las dos finalistas y, para cada una, el mayor riesgo.

## 8. Estructura de un proyecto

Los agentes se comunican a través de archivos con un formato fijo. Así cada uno sabe qué leer y qué
escribir, y todo queda en git.

```
mi-juego/
├── estudio/
│   ├── agentes/              # Instrucciones de cada agente (nadie las modifica salvo la persona)
│   ├── gustos.md             # La persona
│   ├── concepto/
│   │   ├── ideas.md          # Ideadores
│   │   ├── debate.md         # Ideadores
│   │   └── decision.md       # Juez
│   ├── decisiones.md         # Las tres decisiones de la persona
│   ├── gdd.md                # Game Designer
│   ├── guia-estilo.md        # Director de arte
│   ├── paleta.txt            # Director de arte
│   ├── sprites/              # Grafista: sprites como texto
│   ├── audio/                # Compositor: música y efectos como texto
│   ├── backlog/              # Product Owner: una historia por archivo
│   ├── registro.md           # Archivista: cada paso del estudio
│   └── informes/
│       ├── revision/         # Revisor
│       ├── arte/             # Sprites ampliados y revisión del director de arte
│       ├── balance/          # Simulaciones + informe del economista
│       └── playtest/         # Capturas e informe del playtester
├── godot/
│   ├── project.godot
│   ├── scenes/               # Desarrollador
│   ├── scripts/              # Desarrollador: lógica en clases puras
│   ├── data/                 # Diseñador de contenido (JSON)
│   ├── assets/               # PNG y WAV generados por las herramientas
│   ├── tests/                # Tester (GUT)
│   ├── sim/                  # Simulaciones sin interfaz para el economista
│   └── playtest/             # Autoload y guion de entradas del playtest
└── tools/                    # Orquestador y herramientas (Python, sin dependencias)
```

Los tests, las simulaciones y el playtest van dentro de `godot/` porque Godot solo puede cargar archivos
de su propio proyecto (`res://`).

Esta estructura está lista para copiar en [plantillas/estudio-juegos/](../plantillas/estudio-juegos/), y
[ESTUDIO-VIDEOJUEGOS-GUIA.md](ESTUDIO-VIDEOJUEGOS-GUIA.md) explica cómo usarla paso a paso, con o sin
internet.

## 9. Qué corre en Microsoft 365 y qué en local

| Pieza | Dónde | Por qué |
| --- | --- | --- |
| Canal del estudio | **Teams** | Los agentes publican avances y la persona aprueba desde ahí |
| Ideadores, juez, Game Designer, Product Owner, publicista | **Copilot Studio** | Son agentes de "oficina": texto, documentos y pocas iteraciones. Se pueden montar como un agente principal con agentes conectados |
| Backlog | **Planner** | El Product Owner crea y mueve tareas con el conector de Planner |
| GDD, guía de estilo, decisiones | **SharePoint / OneDrive** (o el repo) | Documentos que los agentes de Copilot Studio pueden usar como conocimiento |
| Imágenes | **Copilot** (si se puede usar desde un agente) o un generador local | Ver sección 12 |
| Desarrollador, revisor, tester, playtester, economista | **Local** (Ollama o el modelo que se elija) | Necesitan ejecutar Godot, y su bucle se repite decenas de veces por historia |
| Orquestador, build, export | **Local** (scripts) | Código normal |

El puente entre los dos lados es un servidor MCP local, como el de `devassist-copilot`
([src/devassist_copilot/mcp.py](../src/devassist_copilot/mcp.py)): expone herramientas como
`run_tests`, `run_playtest` o `get_story_status` para que los agentes de Copilot Studio consulten el estado
o lancen el bucle de producción sin ejecutarlo ellos.

## 10. Plan para el mes de prueba

No conviene montar los 16 agentes a la vez. Cada semana añade una capa y termina con algo que se puede
probar.

| Semana | Qué se monta | Se termina cuando… |
| --- | --- | --- |
| **1** | Ideadores A y B y juez en Copilot Studio, publicando en Teams | El estudio propone dos finalistas y la persona elige una (decisión 1) |
| **2** | Game Designer, director de arte y Product Owner con Planner | Hay GDD, guía de estilo aprobada (decisión 2) y un backlog con historias del primer hito |
| **3** | Bucle local: orquestador, desarrollador, revisor y tester con Godot sin interfaz, conectado por MCP | Una historia pasa sola de "pendiente" a "test" con los tests en verde |
| **4** | Gráficos, compositor, efectos, economista, playtester y publicista | Un juego muy pequeño (una pantalla, un bucle) publicado en itch.io tras la decisión 3 |

Si algo se retrasa, lo primero que se recorta es la semana 4: un juego con gráficos simples y sin música
sigue sirviendo para validar el estudio.

## 11. Costes y trampas

- **Créditos.** Los agentes de Copilot Studio que se llaman entre sí y las automatizaciones pueden gastar
  Copilot Credits (detalle en [M365-COPILOT-APRENDER.md](M365-COPILOT-APRENDER.md#7-costes-y-trampas)).
  Por eso los bucles largos (desarrollo, tests, playtest, simulaciones) van en local.
- **Bucles infinitos.** Sin un máximo de intentos, desarrollador y tester pueden pasarse una historia el uno
  al otro sin fin. El orquestador corta y la marca como bloqueada.
- **Debate complaciente.** Si los ideadores no tienen personalidades opuestas, acaban de acuerdo enseguida.
- **Estilo inconsistente.** Toda petición de imagen parte del prompt base de la guía de estilo.
- **Copias.** El juez descarta ideas que sean un juego existente con otro nombre, aunque se presenten
  como originales.
- **Fin de la prueba.** Al terminar el mes, lo que viva solo en Microsoft 365 deja de funcionar. Lo
  importante (GDD, decisiones, código) debe estar también en git.

## 12. Qué no está verificado

- **`godot --headless --import`.** Es la forma de importar assets sin abrir el editor en versiones recientes
  de Godot 4; en versiones antiguas se usa `--headless --editor --quit`. Comprobar con la versión instalada.
- **Generación de imágenes desde Copilot Studio.** Copilot genera imágenes en su propia app, pero no está
  comprobado que un agente de Copilot Studio pueda pedirlas y guardarlas en un archivo. Si no se puede,
  los gráficos se generan en local.
- **Modelos locales con visión.** El director de arte y el playtester necesitan un modelo que entienda
  imágenes. Hay que comprobar qué modelo de Ollama da resultados útiles en el equipo disponible.
- **Calidad de la música por MIDI.** Que un modelo escriba MIDI correcto es fácil; que suene bien, no
  tanto. Es una hipótesis a probar en la semana 4.
