# Microsoft 365 Copilot para desarrolladores: workflows y agentes

Qué se puede construir con una cuenta de Microsoft 365 Copilot para el trabajo de desarrollo, con qué
herramienta, qué licencia o permiso hace falta y por dónde empezar a aprender.

Estado consultado el 11/11/2026. La plataforma cambia casi cada mes: varios nombres de este documento
(Cowork, Code, Work IQ Dev Tools, los *harnesses* de Copilot Studio) tienen menos de un año. Cada dato
lleva su fuente al final. Los que vienen de blogs y no de Microsoft van marcados con *(fuente secundaria)*.

> **Relación con el resto del repo.** [ALTERNATIVAS.md](ALTERNATIVAS.md) compara Copilot Studio y M365
> Copilot como sitio donde *alojar* DevAssist. Este documento es más amplio: todo lo que ofrece la cuenta,
> de lo más sencillo (pedirle cosas a Copilot) a lo más técnico (plugins con MCP).

## Índice

- [1. Qué tienes según la licencia](#1-qué-tienes-según-la-licencia)
- [2. El mapa: de menos a más código](#2-el-mapa-de-menos-a-más-código)
- [3. Cada pieza, explicada](#3-cada-pieza-explicada)
- [4. Casos de uso de desarrollo y qué pieza usar](#4-casos-de-uso-de-desarrollo-y-qué-pieza-usar)
- [5. Ruta de aprendizaje con ejercicios](#5-ruta-de-aprendizaje-con-ejercicios)
- [6. Lo primero: comprobar qué tiene tu cuenta](#6-lo-primero-comprobar-qué-tiene-tu-cuenta)
- [7. Costes y trampas](#7-costes-y-trampas)
- [8. Qué no está verificado](#8-qué-no-está-verificado)
- [Fuentes](#fuentes)

## 1. Qué tienes según la licencia

"Office 365 Copilot" puede significar dos cosas muy distintas. Conviene saber cuál es la tuya antes de nada.

| | **Copilot Chat** (sin licencia extra) | **Microsoft 365 Copilot** (licencia de pago) |
| --- | --- | --- |
| Precio | Incluido en Microsoft 365 | ~30 $/usuario/mes (empresa), ~21 $ (Business) |
| Chat con modelo y web | ✅ | ✅ |
| Copilot dentro de Word, Excel, Outlook, Teams con tus datos | ❌ | ✅ |
| Crear agentes con Agent Builder | ✅ (los que usan datos de la empresa se miden por uso) | ✅ incluido |
| Usar agentes de Copilot Studio en Copilot y Teams | Se miden en créditos | Incluido si el agente actúa con tu identidad (uso justo) |
| Researcher, Analyst, agentes de Microsoft | Depende del tenant (comprobar) | ✅ |
| Workflows, Cowork, Code (programa Frontier) | ❌ | ✅ si estás en Frontier; Cowork y Code gastan créditos además |
| Agent flows de Copilot Studio | Créditos | Incluidos solo con el disparador "cuando un agente llama al flujo" |

Fuera de ambas licencias quedan, y se pagan aparte:

- **Copilot Credits** (Copilot Studio): 0,01 $ por crédito o 200 $/mes por 25.000.
- **Power Automate Premium**: necesario para conectores premium, el disparador HTTP (webhooks) y conectores personalizados.
- **GitHub Copilot**: es otro producto con otra licencia. No viene con Microsoft 365 Copilot.
- **Agent 365**: gobierno de agentes, ~15 $/usuario/mes o incluido en Microsoft 365 E7 *(fuente secundaria)*.

## 2. El mapa: de menos a más código

```
  Sin código ──────────────────────────────────────────────────────────────────▶ Código

  Prompts y      Workflows     Agent        Copilot Studio      Agents Toolkit /     Agents SDK
  Notebooks      (Frontier)    Builder      (low-code)          Work IQ Dev Tools    + Foundry
  Researcher     Cowork                     agent flows         (pro-code)           (tu propio
  Analyst        Power Automate             MCP, computer use   agentes declarativos  orquestador,
                                                                API plugins, MCP     como DevAssist)
                                                                skills, conectores
  ───────────────┬─────────────┬────────────┬──────────────────┬────────────────────┬─────────────
  usar Copilot   automatizar   agente con   agente con lógica   agente/plugin        agente
                 tareas        instrucciones propia y acciones   versionado en git    completamente
                               y conocimiento                    con CI/CD            tuyo
```

Dos ideas que ordenan todo lo demás:

- **Agente declarativo** = el cerebro (modelo y orquestador) es el de Copilot. Tú das instrucciones,
  conocimiento y acciones. Lo crean Agent Builder, Copilot Studio (harness "Copilot chat") y Agents Toolkit.
- **Agente de motor propio (*custom engine*)** = el cerebro es tuyo: eliges modelo y lógica. Es lo que es
  DevAssist (Agents SDK + Foundry). También lo permiten los harnesses Standard y GitHub Copilot de Copilot Studio.

## 3. Cada pieza, explicada

### Copilot como asistente (sin construir nada)

Lo más rentable al principio y lo que menos se aprovecha.

- **Copilot Chat con archivos**: pegar un stack trace, un log o una spec y pedir diagnóstico o resumen.
- **Notebooks / Pages**: reunir specs, actas y correos de un proyecto y preguntar sobre todo a la vez.
- **Researcher**: informes largos con fuentes, de la web y de tus documentos (p. ej. "compara las librerías
  de colas que hemos evaluado según nuestros documentos de arquitectura").
- **Analyst**: análisis de datos con Python por debajo (CSV de métricas, tiempos de pipeline, incidencias).
- **Copilot en Teams**: resumen de reuniones técnicas con decisiones y tareas; "qué me perdí" en un canal.

### Workflows (agente, Frontier)

Automatizaciones en lenguaje natural sobre Outlook, Teams, SharePoint y Planner. Describes el flujo, lo
prueba y lo ves en un diseñador visual. Ejemplo: "cada lunes a las 9, publica en el canal #backend las
tareas de Planner vencidas". Por debajo genera flujos de Power Automate *(fuente secundaria)*.

- Requiere estar en el programa **Frontier** y añadir el agente desde la tienda de agentes.
- Limitación conocida: puede confundirse de canal o sitio si los nombres se parecen.

### Copilot Cowork (Frontier, GA comercial junio 2026)

Le das un resultado ("prepara las notas de la versión 2.3 con los cambios de este mes y envíalas al
equipo") y crea y ejecuta un plan de varios pasos mientras revisas. Admite **automatizaciones recurrentes y
por evento**, **skills** y **plugins**. Consume Copilot Credits además de la licencia *(fuente secundaria)*.

### Copilot Code (Frontier, desde finales de octubre 2026)

Un destino "Code" en la app de Copilot para crear pequeñas aplicaciones y publicarlas internamente en el
*Copilot Managed Runtime*. Sesiones en la nube por defecto; ejecución local solo si el admin la activa.
Puede conectarse a GitHub si el admin lo permite *(fuente secundaria)*. Útil para herramientas internas
rápidas, no para el producto.

### Agent Builder

Dentro del propio Copilot: **Agentes → Crear agente**. Un agente declarativo con:

- **Instrucciones** (hasta ~8.000 caracteres).
- **Conocimiento**: sitios de SharePoint, archivos, correo, Teams, web, conectores de Copilot.
- **Capacidades**: intérprete de código, generación de imágenes.

Sin acciones propias ni lógica. Ideal para "el experto en nuestro código y nuestras normas". Se comparte
con enlace o se pasa a Copilot Studio cuando se queda corto.

### Copilot Studio

El entorno low-code ([copilotstudio.microsoft.com](https://copilotstudio.microsoft.com)). La nueva
experiencia tiene tres *harnesses* *(fuente secundaria)*:

| Harness | Para qué | Publicación |
| --- | --- | --- |
| **Copilot chat** | Extender Microsoft 365 Copilot (agente declarativo) | Solo interno |
| **Standard** | Temas, reglas, flujos: el Copilot Studio "clásico" | Interno o externo |
| **GitHub Copilot** | Tareas largas con mucho razonamiento | Interno o externo |

El nombre "GitHub Copilot" es del harness: no hace falta licencia de GitHub Copilot por usuario.

Lo que interesa a un desarrollador:

- **Herramientas**: conectores de Power Platform (GitHub, Azure DevOps, Jira…), APIs REST, **servidores MCP**,
  prompts, y **computer use** (manejar una web o app de escritorio sin API).
- **Agent flows**: flujos deterministas dentro del agente, con disparadores por evento o programados.
- **Desencadenadores autónomos**: el agente reacciona a un correo, un archivo nuevo, una programación.
- **Agente a agente**: un agente puede llamar a otros, incluidos los de Foundry.
- **Evaluación**: conjuntos de pruebas para medir respuestas antes de publicar.
- **ALM**: soluciones de Power Platform para pasar de desarrollo a producción.

En el repo: la rama [`alt/copilot-studio`](https://github.com/mirijumjum/learning-agents/tree/alt/copilot-studio)
ya tiene un agente (`.mcs.yml`), un agent flow y un backend MCP + REST.

### Power Automate

Los flujos clásicos. Con la licencia de Microsoft 365 solo tienes **conectores estándar** (Outlook, Teams,
SharePoint, Planner y algunos más). Para recibir webhooks (disparador HTTP), llamar a APIs propias o usar
conectores personalizados hace falta **Premium**. Es la pieza que falta en M365 Copilot para reaccionar a
eventos de GitLab o del CI.

### Agents Toolkit (pro-code, VS Code)

Extensión de VS Code y CLI para crear **agentes declarativos como código**:

- `declarativeAgent.json` con instrucciones, conocimiento y capacidades.
- **Acciones**: *API plugins* (desde un OpenAPI) o **servidores MCP** (el toolkit lee las herramientas
  del servidor y genera el manifiesto).
- **MCP Apps**: el servidor MCP puede devolver widgets interactivos que se pintan en el chat (desde abril
  2026, solo en agentes del toolkit, no en Copilot Studio).
- Autenticación por SSO de Entra u OAuth.
- Pruebas en el *playground* local y en tu tenant (hace falta permiso de **carga de apps personalizadas**).

Limitación: las herramientas MCP se declaran de forma estática en el manifiesto. Si el servidor añade una
herramienta, hay que regenerar *(fuente secundaria, junio 2026)*.

En el repo: la rama [`alt/m365-copilot`](https://github.com/mirijumjum/learning-agents/tree/alt/m365-copilot)
tiene un agente declarativo v1.9 con un plugin v2.4.

### Work IQ Dev Tools y los "plugins" (nuevo, preview)

Desde noviembre 2026 la documentación de Microsoft llama **plugin** a un paquete instalable que junta:

| Pieza | Qué aporta |
| --- | --- |
| Agente declarativo | Asistente con instrucciones, conocimiento y herramientas |
| **Skill** | Instrucciones o un procedimiento reutilizable (`skills/<nombre>/SKILL.md`) |
| Conector de Copilot | Contenido externo indexado |
| Servidor MCP | Herramientas de un servidor remoto |

Se construye con la CLI **`wiqd`** (v0.25 preview), que también se instala como plugin de Claude Code o de
GitHub Copilot CLI:

```bash
wiqd auth login --interactive
wiqd doctor
wiqd plugin create --name devassist-lite
wiqd plugin add skill --name "Triage Issues"
wiqd plugin validate --mode source --env dev
wiqd plugin provision --env dev
wiqd plugin package --env dev
wiqd plugin share --env dev --scope users --email tu@empresa.com
```

Es la forma más "de desarrollador" de trabajar: todo en git, con validación y CI/CD. Las skills tienen la
misma idea que las de Claude Code: un fichero Markdown con un procedimiento que el modelo carga cuando toca.

### Conectores de Copilot (antes Graph connectors)

Indexan contenido externo para que Copilot y tus agentes lo busquen: wikis, Confluence, Jira, Azure DevOps,
repositorios, bases de conocimiento propias. Hay conectores ya hechos y una API para crear el tuyo.
Respetan permisos si mapeas las ACL. Para "pregúntale a Copilot por nuestros ADR" es lo que hace falta.

### Agents SDK + Foundry (motor propio)

Lo que ya hiciste con DevAssist: tu código, tu modelo, publicado en Teams y en Copilot. Es la salida cuando
necesitas bucles deterministas (el de tests), webhooks o elegir modelo.

### Gobierno: Agent 365, Entra y el centro de administración

- **Agent Registry / Agent 365**: inventario y políticas de todos los agentes del tenant.
- **Entra Agent ID**: los agentes tienen identidad propia. Desde el 4/11/2026 los usuarios pueden crear
  *agent blueprints* salvo que el admin lo desactive *(fuente secundaria)*.
- **Informe de uso de agentes** en el centro de administración (desde el 4/11/2026), por usuario y agente.

No es para construir, pero condiciona lo que tu admin te dejará hacer.

## 4. Casos de uso de desarrollo y qué pieza usar

| Caso | Pieza recomendada | Por qué |
| --- | --- | --- |
| Preguntar por normas de código, ADR, wiki | Agent Builder + SharePoint o conector | Sin código, en minutos |
| Resumen de la daily o de una reunión técnica con tareas | Copilot en Teams | Ya viene hecho |
| Aviso semanal de tareas vencidas o PRs abiertas en el canal | Workflows o Power Automate (programado) | Disparador por tiempo, conectores estándar |
| Reaccionar a un pipeline fallido (webhook) | Power Automate Premium o motor propio | M365 Copilot no recibe webhooks |
| Triaje de issues con etiquetas y prioridad | Plugin con skill + MCP de GitLab/GitHub | Procedimiento repetible y herramientas |
| Diagnosticar un fallo de CI con logs | Copilot Studio o Agents Toolkit con MCP | Necesita llamar a tu API |
| Notas de versión a partir de commits | Cowork o agente con acción a la API de git | Varios pasos con revisión |
| Onboarding de alguien nuevo al proyecto | Agent Builder con docs + Notebook | Conocimiento, no acciones |
| Bucle escribir código → tests → CI hasta que pasen | Motor propio (DevAssist) | Necesita orden fijo y control |
| Herramienta interna rápida (panel, formulario) | Copilot Code | Se publica sin infraestructura |
| Automatizar una web sin API (portal de un proveedor) | Copilot Studio con computer use | Única opción sin API; cuesta créditos |

## 5. Ruta de aprendizaje con ejercicios

Ordenada para que cada paso necesite lo mínimo de permisos y dinero. Marca qué hace falta en cada uno.

### Nivel 1 · Usar Copilot bien (licencia M365 Copilot o Copilot Chat)

1. Pega un stack trace real de DevAssist en Copilot Chat y pide la causa y un parche.
2. Crea un Notebook con [DEVASSIST.md](DEVASSIST.md), [ALTERNATIVAS.md](ALTERNATIVAS.md) y la guía de Foundry.
   Pregunta "¿qué falta para pasar a producción?" y compara con la sección *Pendiente* del documento.
3. Pide a Researcher un informe sobre MCP en Microsoft 365 con fuentes. Contrasta con este documento.

### Nivel 2 · Tu primer agente (Agent Builder)

4. Crea **"Experto en learning-agents"**: instrucciones con las normas del repo, conocimiento con los
   documentos subidos a OneDrive/SharePoint. Prueba 10 preguntas y anota cuáles falla.
5. Afina las instrucciones (formato de respuesta, cuándo decir "no lo sé") y repite las 10 preguntas.

### Nivel 3 · Automatizar (Workflows o Power Automate)

6. Flujo programado: cada lunes publica en un chat de Teams las tareas de Planner vencidas.
7. Flujo por evento: cuando llegue un correo de alertas con "FAILED" en el asunto, crea una tarea en Planner
   y avisa en Teams. (Sin webhooks: el correo hace de puente.)

### Nivel 4 · Agente con acciones (Copilot Studio)

8. Importa el agente de la rama `alt/copilot-studio` o créalo de cero con el harness *Copilot chat*.
9. Conéctale el **servidor MCP** de esa rama (expuesto con un túnel de desarrollo) y pide "¿por qué falló el
   último pipeline?".
10. Añade un **agent flow** con disparador programado y un **conjunto de evaluación** de 10 casos.

### Nivel 5 · Pro-code (Agents Toolkit y wiqd)

11. Con Agents Toolkit: agente declarativo + acción MCP contra el mismo servidor. Pruébalo en el playground.
12. Con `wiqd`: empaqueta ese agente como **plugin** con una **skill** "Triaje de issues" (un `SKILL.md` con
    el procedimiento) y compártelo contigo mismo.
13. Añade una **MCP App**: que el diagnóstico del pipeline devuelva una tarjeta con los jobs y su estado.
14. Ponlo en CI: `wiqd plugin validate` en cada push.

### Nivel 6 · Comparar con lo que ya tienes

15. Pide la misma tarea (diagnosticar un pipeline) a DevAssist, al agente de Studio y al plugin. Anota calidad,
    latencia, coste y qué controlas en cada uno. Es la prueba que [ALTERNATIVAS.md](ALTERNATIVAS.md) deja
    pendiente: "merece una prueba en un tenant antes de decidir".

## 6. Lo primero: comprobar qué tiene tu cuenta

Antes del nivel 2, en 10 minutos:

- [ ] En [m365.cloud.microsoft/chat](https://m365.cloud.microsoft/chat): ¿aparece el selector *Trabajo/Web*
      y Copilot en Word/Outlook? Si sí, tienes licencia de M365 Copilot. Si solo hay chat web, es Copilot Chat.
- [ ] ¿Aparece **Agentes → Crear agente**? Es Agent Builder.
- [ ] ¿Ves *Cowork*, *Code* o el agente *Workflows* en la tienda? Estás en Frontier.
- [ ] Entra en [copilotstudio.microsoft.com](https://copilotstudio.microsoft.com). ¿Puedes crear un agente
      y elegir entorno? ¿Hay facturación de créditos configurada?
- [ ] En [make.powerautomate.com](https://make.powerautomate.com), al crear un flujo con disparador
      *"Cuando se recibe una solicitud HTTP"*, ¿lo marca como Premium?
- [ ] En Teams: **Aplicaciones → Administrar aplicaciones → Cargar una aplicación**. Si no aparece
      *"Cargar una aplicación personalizada"*, el admin la tiene desactivada y no podrás probar el nivel 5
      en ese tenant.

Si la cuenta es del trabajo y faltan permisos, lo habitual es pedir al admin un **entorno de desarrollo**
de Power Platform y la carga de apps personalizadas solo para ti.

## 7. Costes y trampas

- **Un crédito no es un mensaje.** Una interacción en el harness Standard puede gastar varios contadores:
  respuesta generativa 2, acción 5, consulta a datos del tenant 10 *(fuente secundaria)*. Mira el informe
  de consumo tras cada prueba.
- **Lo "incluido" tiene letra pequeña**: solo con la identidad del usuario con licencia, con uso justo, y en
  agent flows solo con el disparador "cuando un agente llama al flujo". Los disparadores programados o por
  evento y computer use gastan créditos.
- **Probar también gasta**: en el harness GitHub Copilot, crear, previsualizar y evaluar consume créditos
  antes de publicar *(fuente secundaria)*.
- **Modelos con uso medido**: desde noviembre 2026 aparecen en el selector modelos de facturación por uso
  (p. ej. GPT-6 Astra, Claude Fable 5.1) solo si el admin activa UBB y asigna créditos *(fuente secundaria)*.
- **Frontier no es estable**: Workflows, Cowork y Code pueden cambiar o desaparecer. Targeted release se
  retira en enero 2027 y Frontier pasa a ser la vía para lo nuevo.
- **Sin webhooks en M365 Copilot**: para reaccionar al CI hace falta Power Automate Premium o un motor propio.
- **Límites de agentes declarativos**: ~8.000 caracteres de instrucciones, OpenAPI sin objetos anidados
  profundos, herramientas MCP estáticas.

## 8. Qué no está verificado

Nada de este documento se ha probado en un tenant. En concreto, hay que confirmar:

- Qué incluye tu licencia exacta (empresa, Business, E5, E7) y si estás en Frontier.
- Los precios por crédito de cada acción y si computer use o MCP tienen tarifa propia.
- Si Copilot Studio ya pinta MCP Apps, y si las herramientas MCP siguen siendo estáticas.
- Cómo se relacionan Agents Toolkit y `wiqd`: la documentación de `wiqd` no menciona el toolkit.

## Fuentes

Microsoft (oficiales):

- [Extending Microsoft 365 Copilot with plugins](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/) (actualizado 05/11/2026)
- [What is a Microsoft 365 Copilot plugin?](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugins-overview)
- [Build a plugin with Work IQ Dev Tools](https://microsoft.github.io/wiqd/getting-started/build-a-plugin/)
- [Release notes de Microsoft 365 Copilot](https://learn.microsoft.com/en-us/microsoft-365/copilot/release-notes)
- [Novedades de la guía de Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/whats-new)
- [Introducción a Workflows en Microsoft 365 Copilot](https://support.microsoft.com/es-ES/Microsoft-365-Copilot/get-started-with-workflows-in-microsoft-365-copilot)
- [Build declarative agents with MCP](https://devblogs.microsoft.com/microsoft365dev/build-declarative-agents-for-microsoft-365-copilot-with-mcp/) (dic. 2025)
- [MCP Apps now available in Copilot Chat](https://devblogs.microsoft.com/microsoft365dev/mcp-apps-now-available-in-copilot-chat/) (abr. 2026)

Secundarias (contrastar antes de decidir):

- [Copilot vs Cowork vs agents — Jannik Reinhard](https://jannikreinhard.com/copilot-vs-cowork-vs-agents/) (licencias, harnesses, créditos; 16/09/2026)
- [Microsoft 365 November 2026 updates — ChangePilot](https://changepilot.cloud/blog/november-2026-microsoft-365-updates-copilot-billing-code-more) (Code, UBB, Entra)
- [Copilot Studio Licensing Guide, octubre 2026 — Licensing School](https://www.licensingschool.co.uk/wp-content/uploads/2026/10/Microsoft-Copilot-Studio-Licensing-Guide-October-2026-v3.pdf)
- [Declarative agents what's new, abril 2026 — Andrew Connell](https://www.andrewconnell.com/articles/microsoft-365-copilot-declarative-agents-whats-new-202604-april-2026/)
- [Copilot Weekly, 09/11/2026 — Big Hat Group](https://www.bighatgroup.com/blog/copilot-weekly-2026-11-09/)
