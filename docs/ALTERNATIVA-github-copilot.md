# Alternativa: GitHub Copilot (Business/Enterprise) en lugar de agentes propios en Foundry

Rama `alt/github-copilot`. Compara con la guía [Teams-AI-Agents-Microsoft-Foundry.md](../Teams-AI-Agents-Microsoft-Foundry.md)
y su implementación [DEVASSIST.md](DEVASSIST.md). Precios y documentación consultados el **10 de noviembre de 2026**.

## Índice

- [Qué es](#qué-es)
- [Qué cubre de la guía](#qué-cubre-de-la-guía)
- [El problema de GitLab](#el-problema-de-gitlab)
- [Suscripciones y licencias](#suscripciones-y-licencias)
- [Coste estimado para 5 y 20 desarrolladores](#coste-estimado-para-5-y-20-desarrolladores)
- [Pros](#pros)
- [Contras](#contras)
- [Despliegue paso a paso](#despliegue-paso-a-paso)
- [Qué hay en esta rama](#qué-hay-en-esta-rama)
- [Qué está verificado aquí y qué no](#qué-está-verificado-aquí-y-qué-no)
- [Fuentes](#fuentes)

## Qué es

En vez de construir y mantener DevAssist (orquestador y cuatro especialistas en Python, desplegados en Azure),
se **compran funciones ya hechas de GitHub Copilot** y se configuran con ficheros del repositorio:

| Pieza de la guía | Pieza de GitHub Copilot que la sustituye |
| --- | --- |
| Orquestador DevAssist | Copilot cloud agent (antes "coding agent"), que se lanza asignando un issue, desde la pestaña **Agents** o con `@GitHub` en Teams |
| Cuatro especialistas con instrucciones propias | **Custom agents** en `.github/agents/*.agent.md` (aquí: `devassist-tests`, `devassist-docs`, `devassist-git`, `devassist-trace`) |
| Reglas del equipo | **Custom instructions**: `.github/copilot-instructions.md` y `.github/instructions/*.instructions.md` |
| Revisión automática de MRs | **Copilot code review**, activado por un ruleset |
| Entorno donde se ejecutan los tests | `copilot-setup-steps.yml`: un runner de GitHub Actions con el proyecto instalado |
| "¿Por qué está rojo el pipeline?" | Custom agent de trazas y, de forma automática, **Copilot CLI dentro de GitHub Actions** |
| Teams como interfaz | La **app de GitHub para Microsoft Teams**, que lanza sesiones de Copilot cloud agent |
| Webhooks de GitLab | **Copilot automations** (al abrir PR, al crear issue, programadas) y workflows de Actions |

El cambio de fondo es que **no hay código propio que mantener ni infraestructura Azure**. A cambio, todo
pasa a depender de que el código esté en **GitHub**, y la empresa usa **GitLab** (ver [El problema de GitLab](#el-problema-de-gitlab)).

## Qué cubre de la guía

Leyenda: ✅ cubierto · 🟡 parcial · ❌ no.

La columna "GitHub" supone que el repositorio está en GitHub (migrado). La columna "GitLab" supone que
el código sigue en GitLab, con espejo hacia GitHub o con Copilot solo en el IDE.

| Punto de la guía | GitHub | GitLab | Por qué |
| --- | --- | --- | --- |
| **Agente de tests** (escribe, ejecuta en CI, corrige hasta 3 veces, abre MR) | 🟡 | ❌ | Copilot cloud agent escribe los tests, los ejecuta con pytest en su runner de Actions y abre una PR desde `copilot/*`. Pero el bucle "hasta 3 intentos" y "si el código está mal, para" son **instrucciones** del custom agent, no código como en DevAssist: el modelo puede saltárselas. La cobertura antes/después la mide pytest-cov si el agente lo ejecuta. Sobre GitLab no puede abrir MRs: con un espejo, la PR queda en GitHub y alguien tiene que llevar la rama a GitLab a mano. |
| **Agente de documentación** (`docs/api.md`, README, changelog, docstrings, entregado como MR) | 🟡 | ❌ | Lo hace el custom agent `devassist-docs`. La comprobación de "solo cambian docstrings" se hace con [`scripts/check_doc_only_changes.py`](../scripts/check_doc_only_changes.py), pero es el agente quien decide ejecutarla. El changelog "al fusionar" no tiene disparador documentado en Copilot automations. |
| **Agente Git** (ramas, MRs, comentarios, issues, estado de pipelines) | 🟡 | ❌ | Leer PRs, ramas, issues y estado de workflows: sí, con las herramientas `github/*`, que son **de solo lectura**. Crear una rama con nombre libre (`feature/invoice-pdf`) o comentar: el cloud agent no puede (solo escribe en su rama `copilot/*`); en Teams, la app de GitHub tiene sus propios comandos. Nada de esto funciona contra la API de GitLab. |
| **Agente de trazas** (logs por rango de tiempo, pipelines rojos, stack traces, borrador de issue) | 🟡 | ❌ | Workflows rojos de GitHub Actions y stack traces pegados: sí (custom agent y workflow automático con enmascarado). Logs de la aplicación en Application Insights o Elasticsearch: **no** sin añadir un servidor MCP con credenciales, que no se ha preparado. Pipelines de GitLab CI: no. |
| **Interfaz Teams** | 🟡 | ❌ | `@GitHub <tarea>` en un canal, chat o mensaje directo lanza una sesión de cloud agent (hasta 11 repos del mismo propietario) y contesta en el hilo con la PR. Cada usuario necesita **licencia de Copilot de pago**, cuenta de GitHub vinculada y permiso de escritura en el repo. La documentación no dice cómo elegir un custom agent desde Teams. En la guía, el usuario de Teams no necesita ninguna licencia. |
| **Microsoft 365 Copilot** (publicar el agente en Copilot Chat) | ❌ | ❌ | GitHub Copilot es otro producto. No se publica como agente en Microsoft 365 Copilot. |
| **Webhooks / automatizaciones** (tests al abrir MR, changelog al fusionar, causa al fallar pipeline) | 🟡 | ❌ | Al abrir PR: Copilot code review automático (ruleset) y Copilot automations ("When a pull request is opened"), que se configuran en la web, no en ficheros, y solo en repos privados o internos. Al fallar CI: workflow [`copilot-trace-on-failure.yml`](../.github/workflows/copilot-trace-on-failure.yml), **apagado por defecto**. Al fusionar: sin disparador documentado; habría que usar GitHub Agentic Workflows o Actions. |
| **Garantías de seguridad** (nunca fusionar, desplegar ni borrar; solo ramas de agente; enmascarado; solo miembros del tenant) | 🟡 | 🟡 | Las da la **plataforma**, no un prompt: el cloud agent solo puede hacer push a su rama `copilot/*`, no puede aprobar ni fusionar, los workflows de sus PRs esperan aprobación, tiene cortafuegos y solo lo lanzan usuarios con escritura. El ruleset recomendado exige revisión humana. **Pero** el enmascarado de datos personales solo es automático en el workflow de trazas; en una sesión interactiva el modelo ve lo que el usuario pegue. Si se activa "Copilot approvals", Copilot podría contar como aprobación: hay que dejarlo desactivado. En el IDE (opción GitLab) solo quedan las protecciones de GitLab. |
| **Datos en la UE** | 🟡 | 🟡 | Solo con **GitHub Enterprise Cloud con residencia de datos** (`*.ghe.com`, región UE) y la política "Restrict Copilot to data residency compliant models", que encarece un **10 %** el consumo de créditos. En github.com normal, la inferencia y los logs de Copilot pueden salir de la UE. No está documentado que la app de Teams funcione con GHE.com. |
| **Auditoría / coste** | ✅ | 🟡 | Logs de sesión del agente, eventos de auditoría, API de métricas de uso y **presupuestos en dólares** con corte duro (por usuario, centro de coste u organización). No hay registro propio de tokens por petición como `devassist usage`. En el IDE solo se ven las métricas de uso de Copilot. |

## El problema de GitLab

GitHub lo dice sin rodeos: Copilot cloud agent *solo funciona con repositorios alojados en GitHub*. Lo mismo vale
para code review, custom agents en la web, automations y la app de Teams. Hay tres caminos:

| Opción | Cómo | Qué se consigue | Consecuencias |
| --- | --- | --- | --- |
| **1. Migrar a GitHub** | GitHub Importer o GitHub Enterprise Importer; mover CI de `.gitlab-ci.yml` a Actions | Todo lo de la columna "GitHub" | Proyecto de migración (repos, CI, runners, permisos, integraciones). Hay que pagar GitHub Team o Enterprise además de Copilot. Se abandona la inversión en GitLab. |
| **2. Espejo GitLab → GitHub** | *Push mirror* de GitLab (disponible en Free, Premium y Ultimate) con un token de GitHub de grano fino | Copilot puede **leer** el código y trabajar sobre la copia; code review y CI en las PRs de GitHub | El espejo es **de un solo sentido**: GitHub es una copia pasiva y cualquier cambio hecho allí se pierde o diverge. Las PRs de Copilot quedan en GitHub y hay que llevarlas a GitLab a mano (`git fetch` de la rama `copilot/*` y abrir MR). Hay dos sitios con el código y dos sistemas de permisos. El espejo se actualiza en unos 5 minutos (1 si solo se reflejan ramas protegidas). No se recomienda salvo como piloto de solo lectura. |
| **3. Copilot solo en el IDE** | Copilot Business para cada desarrollador, en VS Code o JetBrains, sobre el clon de GitLab | Autocompletado ilimitado, chat y modo agente en local. Los custom agents y las instrucciones de esta rama **también sirven en VS Code** | No hay Teams, ni cloud agent, ni revisión de MRs, ni automatizaciones. Los tests se ejecutan en el portátil, no en GitLab CI. Ninguna garantía de plataforma: el usuario aprueba cada comando. Es un asistente personal, no un sustituto de DevAssist. |

## Suscripciones y licencias

Precios de lista en USD, consultados el **10-11-2026**. Desde el **1 de junio de 2026** Copilot cobra en
**GitHub AI Credits** (1 crédito = 0,01 $) según los tokens consumidos; las antiguas "premium requests" desaparecieron.

| Producto | Precio | Qué incluye | Enlace |
| --- | --- | --- | --- |
| Copilot Free | 0 $ | Cupo limitado; sin cloud agent ni code review | [Plans for GitHub Copilot](https://docs.github.com/en/copilot/get-started/plans) |
| Copilot Pro (individual) | 10 $/mes | 1.500 créditos (1.000 + 500 "flex"), cloud agent y code review | [Plans](https://docs.github.com/en/copilot/get-started/plans) |
| Copilot Pro+ (individual) | 39 $/mes | 7.000 créditos | [Plans](https://docs.github.com/en/copilot/get-started/plans) |
| **Copilot Business** | **19 $/usuario/mes** | **1.900 créditos/usuario/mes**, compartidos en una bolsa común de la organización; cloud agent, code review, custom instructions, MCP, agentes de terceros | [Plans](https://docs.github.com/en/copilot/get-started/plans), [Usage-based billing for organizations](https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises) |
| Copilot Enterprise | 39 $/usuario/mes | 3.900 créditos/usuario/mes; requiere GitHub Enterprise Cloud | Mismos enlaces |
| Créditos adicionales | 0,01 $/crédito | Se consumen al agotar la bolsa. **El pago por uso está activado por defecto** en Business y Enterprise: hay que poner presupuesto | [Usage-based billing](https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises) |
| Precio por tokens (ejemplos) | GPT-5.3-Codex: 1,75 $ entrada / 14 $ salida por 1M; Claude Sonnet 5: 2 $ / 10 $; GPT-5.4 nano: 0,20 $ / 1,25 $ | Lo que descuenta cada sesión de la bolsa | [Models and pricing](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing) |
| Copilot code review | Media estimada por revisión: Lite 0,25 $, Balanced 1,50 $, Max 10 $ (rangos 0,05–1 $, 0,25–5 $, 1,50–25 $), **más minutos de Actions** | | [Models and pricing](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing), [Code review](https://docs.github.com/en/copilot/concepts/agents/code-review) |
| Copilot cloud agent | Créditos según tokens **más minutos de Actions** del runner; máximo 59 min por sesión | | [Cloud agent](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent) |
| GitHub Free (organización) | 0 $ | 2.000 min de Actions al mes (12 $ de cómputo). Sin reglas de protección en repos privados | [GitHub pricing](https://github.com/pricing) |
| **GitHub Team** | **4 $/usuario/mes** | 3.000 min de Actions al mes (18 $), rulesets en repos privados | [GitHub pricing](https://github.com/pricing), [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) |
| GitHub Enterprise Cloud | 21 $/usuario/mes | 50.000 min de Actions (300 $); opción de residencia de datos en la UE (`*.ghe.com`) | [GitHub pricing](https://github.com/pricing), [Data residency](https://docs.github.com/en/enterprise-cloud@latest/admin/data-residency/about-github-enterprise-cloud-with-data-residency) |
| Residencia de datos para Copilot | +10 % en el consumo de créditos | Inferencia, prompts, respuestas y logs en la región | [Copilot with data residency](https://docs.github.com/en/enterprise-cloud@latest/admin/data-residency/github-copilot-with-data-residency) |
| Minutos de Actions | Linux 2 núcleos: 0,006 $/min; Windows: 0,010 $/min. **Gratis en repos públicos** con runners estándar | | [Actions runner pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing) |
| App de GitHub para Teams | Sin coste propio. Cada usuario que lanza tareas necesita un plan de Copilot de pago | | [Integrating cloud agent with Teams](https://docs.github.com/en/copilot/how-tos/copilot-integrations/integrate-cloud-agent-with-teams) |

## Coste estimado para 5 y 20 desarrolladores

**Supuestos** (estimación propia, no medida):

- La misma carga que la guía para 20 personas, al mes: 200 peticiones de tests, 100 de documentación,
  300 revisiones automáticas, 300 preguntas de trazas y 1.000 operaciones Git. Para 5 personas, una cuarta parte.
- Una sesión de cloud agent (tests o docs) cuesta **unos 1 $ en créditos** (≈1M de tokens de entrada, el 80 % en caché,
  y 30k de salida con un modelo de 2 $/10 $ por millón). Un agente autónomo lee y reintenta mucho más que el bucle
  fijo de DevAssist, así que este número es la mayor incertidumbre: puede ser la mitad o el triple.
- Revisiones: tarifa media publicada de **Balanced (1,50 $)**; en paréntesis, con **Lite (0,25 $)**.
- Trazas: 0,30 $ por pregunta (mezcla de chat y sesiones cortas). Operaciones Git en chat o Teams: 0,05 $.
- Actions: unos 20 min por sesión de agente, 5 min por revisión y 3 min por ejecución de CI (Linux, 0,006 $/min).
- Opción 1 (migrar): GitHub Team + Copilot Business. Opción 3: solo Copilot Business en una organización GitHub Free.

| Concepto | 5 desarrolladores | 20 desarrolladores |
| --- | --- | --- |
| Copilot Business (19 $/usuario) | 95 $ | 380 $ |
| GitHub Team (4 $/usuario), solo opción 1 | 20 $ | 80 $ |
| Créditos consumidos (Balanced / Lite) | ≈ 223 $ / 129 $ | ≈ 890 $ / 515 $ |
| − Bolsa incluida (1.900 créditos × usuarios) | − 95 $ | − 380 $ |
| = Créditos extra | ≈ 128 $ / 34 $ | ≈ 510 $ / 135 $ |
| Actions (≈2.300 / ≈9.000 min; Team incluye ≈3.000) | 0 $ | ≈ 36 $ |
| **Total opción 1 (todo en GitHub)** | **≈ 150–245 $/mes** | **≈ 630–1.005 $/mes** |
| **Total opción 3 (solo IDE, GitLab intacto)** | **≈ 95 $/mes** + uso extra del modo agente | **≈ 380 $/mes** + uso extra del modo agente |
| Con residencia de datos UE (Enterprise Cloud 21 $/usuario en vez de Team, créditos +10 %) | ≈ 245–350 $/mes | ≈ 990–1.400 $/mes |
| **Guía (Foundry, comparación)** | **≈ 40 $/mes** | **≈ 155 $/mes** |

Lectura rápida: el precio por puesto de la guía (19 $) es **solo el suelo**. Con el cobro por tokens desde junio de 2026,
la parte agéntica (cloud agent y code review) es la que sube la factura. Para acotarla: revisiones en **Lite**, presupuesto
con corte duro y la política "AI credits paid usage" desactivada hasta tener datos reales del piloto.

## Pros

- **Nada que construir ni operar**: ni contenedor, ni Bicep, ni Key Vault, ni Bot Service. GitHub mantiene el agente, los modelos y Teams.
- **Las garantías clave las impone la plataforma**, no un prompt: rama `copilot/*` única, no puede aprobar ni fusionar, los workflows de sus PRs esperan aprobación, cortafuegos y solo lanzan tareas usuarios con escritura.
- **Entorno real**: los tests se ejecutan en un runner con el proyecto instalado (`copilot-setup-steps.yml`), igual que en CI.
- **Personalización versionada en el repo** (instrucciones y custom agents), revisada por PR, como pide la guía.
- **Varios modelos** (OpenAI, Anthropic, Google) sin desplegar nada.
- **Además del agente**, cada desarrollador recibe autocompletado ilimitado, chat y modo agente en el IDE.
- **Presupuestos en dólares con corte duro** y métricas de uso integradas.
- Los custom agents y las instrucciones son portables a VS Code, así que sirven también en la opción "solo IDE".

## Contras

- **GitLab**: casi todo exige que el código viva en GitHub. Con GitLab solo queda el IDE o un espejo de un sentido incómodo.
- **Más caro** que la guía: 95–245 $ frente a 40 $ (5 devs) y 380–1.005 $ frente a 155 $ (20 devs), más una migración.
- **Licencia por usuario**: en la guía, cualquiera del tenant usa DevAssist desde Teams sin licencia; aquí cada persona necesita Copilot de pago y cuenta de GitHub.
- **Bucles por instrucción, no por código**: los 3 intentos, "no tocar el código de producción" y los números del informe dependen de que el modelo obedezca. DevAssist lo garantiza en Python.
- **Enmascarado de datos personales** solo donde lo pones tú (aquí, el workflow de trazas). No hay filtro general antes del modelo.
- **Sin acceso a logs de producción** (App Insights, Elasticsearch) salvo que se añada un servidor MCP con credenciales.
- **Coste variable y difícil de prever**: cobro por tokens, pago extra activado por defecto y minutos de Actions aparte.
- **UE**: solo con Enterprise Cloud con residencia de datos (21 $/usuario más), y sin confirmar para la app de Teams.
- **No llega a Microsoft 365 Copilot Chat.**
- Copilot automations no son código (se configuran en la web y no se versionan) y solo funcionan en repos privados o internos.

## Despliegue paso a paso

Suponiendo la **opción 1** (repos en una organización de GitHub). Ninguno de estos pasos se ha ejecutado en esta rama.

1. **Organización y plan.** Crear la organización de GitHub (Team o Enterprise Cloud; Enterprise Cloud con residencia de datos si la UE es obligatoria) y migrar el repositorio con su historial.
2. **Copilot Business.** Contratarlo y asignar puestos solo al grupo piloto. En *AI Controls*: dejar activada la política de Copilot cloud agent; **desactivar "AI credits paid usage"** o poner un presupuesto con "Stop usage when budget limit is reached" (sugerido: 100 $ para el piloto).
3. **Ficheros de esta rama** en la rama por defecto: `.github/copilot-instructions.md`, `.github/instructions/`, `.github/agents/`, `.github/workflows/copilot-setup-steps.yml` y `.github/workflows/ci.yml`. El setup solo funciona desde la rama por defecto.
4. **Probar el entorno**: *Actions → Copilot Setup Steps → Run workflow*. Debe quedar en verde.
5. **Ruleset**: *Settings → Rules → Rulesets → New → Import a ruleset* con [`docs/github-copilot/ruleset-main.json`](github-copilot/ruleset-main.json), o por API:

   ```bash
   gh api -X POST repos/OWNER/REPO/rulesets --input docs/github-copilot/ruleset-main.json
   ```

   Exige PR con 1 aprobación humana (y una extra si el cambio no se atribuye a una persona), conversaciones resueltas, el check `pytest` en verde, prohíbe borrar y forzar `main`, y pide revisión de Copilot en cada push. **Sin actores de bypass**: no concedas bypass a Copilot.
6. **Ajustes de Copilot en el repo**: dejar **desactivadas** las aprobaciones de Copilot (para que nunca cuente como revisor), mantener "workflows de PRs de Copilot requieren aprobación" (es el valor por defecto) y el cortafuegos activado con la lista recomendada. Revisión en nivel **Lite** para el piloto.
7. **Teams**: instalar la app de GitHub en el equipo del piloto (enlace de instalación de la documentación → *Add*), y que cada miembro vincule su cuenta. Configurar el repositorio por defecto del canal. Probar con `@GitHub repo=ORG/REPO añade tests para OrderService`.
8. **Custom agents**: en *Agents* o al asignar un issue a Copilot, elegir `devassist-tests`, `devassist-docs`, `devassist-git` o `devassist-trace` en el desplegable.
9. **Automatizaciones** (opcional): en la pestaña *Agents → Automations*, crear "When a pull request is opened" con el agente de tests, con límite de créditos por ejecución. Para el diagnóstico automático de CI, crear la variable de repositorio `COPILOT_TRACE_ENABLED=true` (activa [`copilot-trace-on-failure.yml`](../.github/workflows/copilot-trace-on-failure.yml)) y comprobar que la política "Allow use of Copilot CLI billed to the organization" está activa.
10. **Medir 4–6 semanas**: créditos por sesión, PRs `copilot/*` aceptadas o cerradas, coste frente al presupuesto. Con esos datos, rehacer la tabla de costes.

Para la **opción 3 (solo IDE)**: pasos 2 y 3 (los ficheros pueden vivir en el repo de GitLab: VS Code lee `.github/copilot-instructions.md` y `.github/agents/`), sin pasos de GitHub Actions, ruleset ni Teams.

## Qué hay en esta rama

| Fichero | Para qué |
| --- | --- |
| [`.github/copilot-instructions.md`](../.github/copilot-instructions.md) | Reglas del repositorio: cómo instalar y probar, ramas `copilot/*`/`ai/*`, nunca fusionar ni borrar, no debilitar `gitlab.py` ni `masking.py` |
| [`.github/instructions/`](../.github/instructions/) | Reglas por ruta: `tests/**/*.py`, `src/**/*.py`, documentación |
| [`.github/agents/devassist-*.agent.md`](../.github/agents/) | Los cuatro especialistas, con el bucle de tests, el formato de informe y las mismas prohibiciones que DevAssist |
| [`.github/workflows/copilot-setup-steps.yml`](../.github/workflows/copilot-setup-steps.yml) | Instala el proyecto con `.[dev]` y ejecuta pytest antes de que trabaje Copilot |
| [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) | `python -m pytest -q` y `devassist eval --profile mock`, sin secretos |
| [`.github/workflows/copilot-trace-on-failure.yml`](../.github/workflows/copilot-trace-on-failure.yml) | Diagnóstico automático de CI con Copilot CLI sobre el log **enmascarado**. Apagado salvo `COPILOT_TRACE_ENABLED=true`; ignora forks; Copilot no recibe herramientas |
| [`docs/github-copilot/ruleset-main.json`](github-copilot/ruleset-main.json) | Ruleset recomendado para `main` (formato de la API REST de rulesets) |
| [`scripts/check_doc_only_changes.py`](../scripts/check_doc_only_changes.py) | Comprueba que un cambio de docstrings no toca código (lo usa el agente de docs) |
| [`tests/test_github_copilot_config.py`](../tests/test_github_copilot_config.py) | Valida todo lo anterior contra las reglas de la documentación |

Los workflows son seguros de subir: no usan secretos, solo runners estándar `ubuntu-latest` (gratis en repos públicos) y el único que gasta créditos de Copilot no se ejecuta sin la variable.

## Qué está verificado aquí y qué no

**Verificado en esta máquina (Windows, Python 3.11):**

- `python -m pytest -q`: **121 tests pasan** (los 97 de antes más 24 nuevos de configuración). `devassist eval --profile mock`: 7/7.
- Los 24 tests nuevos comprueban, con PyYAML y las reglas leídas en GitHub Docs: claves válidas y `description` en los custom agents, alias de herramientas conocidos, cuerpo de 30.000 caracteres como máximo, que cada agente mantiene las prohibiciones, que el agente Git no puede editar ni ejecutar, `applyTo`/`excludeAgent` en las instrucciones por ruta y que sus patrones encuentran ficheros reales, que `copilot-setup-steps` tiene un único job con ese nombre, solo claves admitidas y `timeout-minutes` ≤ 59, que ningún workflow usa secretos ni `pull_request_target` y todos tienen permisos explícitos, que el workflow de trazas está apagado por defecto y enmascara antes de llamar al modelo, y que el ruleset solo usa tipos y parámetros documentados y exige el check de CI que existe.
- El comando de enmascarado del workflow de trazas, probado con un log de ejemplo (correo, IP y token salen como `<email>`, `<ip>` y `<secret>`).
- `scripts/check_doc_only_changes.py`: acepta docstrings y comentarios, rechaza cambios de código (tests) y detecta ficheros nuevos.
- Precios y comportamiento contrastados en las páginas oficiales de GitHub Docs, GitHub Blog y github.com/pricing el 10-11-2026.

**No verificado (requiere pagar, crear cuentas o tocar ajustes de GitHub):**

- Nada se ha ejecutado en GitHub: ni los workflows en Actions, ni Copilot cloud agent, ni code review, ni la app de Teams, ni el import del ruleset. La rama solo se ha subido.
- No se ha pasado `actionlint` ni el esquema oficial de workflows; la validación es la de los tests propios.
- Que el permiso `copilot-requests: write` y la invocación de `copilot -p` funcionen igual en el runner (están copiados de la documentación). La versión de `@github/copilot` no está fijada.
- Que los custom agents se puedan elegir desde Teams (la documentación no lo dice).
- Que la app de Teams funcione con GitHub Enterprise Cloud con residencia de datos (`*.ghe.com`).
- El coste real por sesión de cloud agent: los 1 $ por sesión son una suposición. Es lo primero que hay que medir en un piloto.
- `integration_id: 15368` (la app de GitHub Actions) en el ruleset es el valor habitual, pero no se ha comprobado contra la API.
- Las promociones de créditos de junio a agosto de 2026 ya terminaron; los precios pueden volver a cambiar.

## Fuentes

Consultadas el 10-11-2026.

- [Plans for GitHub Copilot](https://docs.github.com/en/copilot/get-started/plans)
- [Usage-based billing for organizations and enterprises](https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises)
- [Models and pricing for GitHub Copilot](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing)
- [GitHub Copilot is moving to usage-based billing](https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/) (GitHub Blog, 27-04-2026)
- [GitHub Copilot on GitHub.com (cloud agent)](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent)
- [Risks and mitigations for Copilot cloud agent](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/risks-and-mitigations)
- [Configure the development environment (copilot-setup-steps)](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/customize-the-agent-environment)
- [Customizing or disabling the firewall](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/customize-the-agent-firewall)
- [Adding repository custom instructions](https://docs.github.com/en/copilot/how-tos/configure-custom-instructions/add-repository-instructions)
- [Custom agents configuration](https://docs.github.com/en/copilot/reference/custom-agents-configuration) y [Creating custom agents for cloud agent](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/create-custom-agents)
- [Copilot code review](https://docs.github.com/en/copilot/concepts/agents/code-review)
- [Using Copilot CLI in GitHub Actions](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli-in-actions)
- [Copilot automations](https://docs.github.com/en/copilot/concepts/agent-capabilities/agent-powered-workflows-and-operations/about-automations) y [GitHub Agentic Workflows](https://docs.github.com/en/copilot/concepts/agent-capabilities/agent-powered-workflows-and-operations/about-github-agentic-workflows)
- [Integrating Copilot cloud agent with Teams](https://docs.github.com/en/copilot/how-tos/copilot-integrations/integrate-cloud-agent-with-teams)
- [GitHub Copilot with data residency](https://docs.github.com/en/enterprise-cloud@latest/admin/data-residency/github-copilot-with-data-residency)
- [REST API: repository rulesets](https://docs.github.com/en/rest/repos/rules)
- [GitHub pricing](https://github.com/pricing), [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [Actions runner pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing)
- [GitLab push mirroring](https://docs.gitlab.com/user/project/repository/mirror/push/)
