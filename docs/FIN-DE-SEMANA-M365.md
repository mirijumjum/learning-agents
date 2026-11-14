# Un fin de semana: DevAssist en Teams real, con workflows y desde el IDE

Guía paso a paso para probar, con pruebas gratuitas, cómo se conecta un agente de desarrollo a
**Teams**, a **workflows** (Teams Workflows y Power Automate) y al **IDE** (VS Code, Visual Studio y
Claude Code por MCP). El agente es DevAssist, que ya funciona en local: ver [DEVASSIST.md](DEVASSIST.md).

Contexto y alternativas: [M365-COPILOT-APRENDER.md](M365-COPILOT-APRENDER.md).

> ⚠️ **Esta guía no se ha seguido aún en un tenant real.** Lo probado es local: tests automáticos y el
> servidor MCP/REST contra el GitLab de pruebas. Los nombres de menús del portal de Azure, de Teams y del
> centro de administración cambian a menudo; si no coinciden, busca la opción por su nombre.

## Índice

- [Qué tendrás el domingo](#qué-tendrás-el-domingo)
- [Coste y fechas a vigilar](#coste-y-fechas-a-vigilar)
- [Viernes: cuentas e instalación](#viernes-cuentas-e-instalación)
- [Sábado mañana: DevAssist en Teams](#sábado-mañana-devassist-en-teams)
- [Sábado tarde: workflows](#sábado-tarde-workflows)
- [Domingo mañana: desde el IDE](#domingo-mañana-desde-el-ide)
- [Domingo tarde: extras opcionales](#domingo-tarde-extras-opcionales)
- [Al terminar: recoger](#al-terminar-recoger)
- [Problemas típicos](#problemas-típicos)

## Qué tendrás el domingo

```
                              ┌──────────── tu PC ─────────────────────────────────────┐
 Teams (chat o canal) ──┐     │                                                         │
                        ▼     │                                                         │
                  Azure Bot ──┼─▶ dev tunnel ─▶ devassist serve :3978 ──▶ Ollama        │
                  (gratis F0) │   (https)          │  /api/messages        (qwen3)      │
                              │                    │  /webhooks/gitlab                  │
 devassist simulate-webhook ──┼────────────────────┘        │                           │
 (pipeline fallido simulado)  │                             ▼                           │
                              │                     devassist fake-gitlab :8929         │
 Teams Workflows ◀────────────┼── tarjeta con el diagnóstico (TEAMS_NOTIFY_WEBHOOK_URL)  │
 (webhook → canal)            │                                                         │
                              │                                                         │
 VS Code / Visual Studio ─────┼─▶ devassist-copilot serve :3981  (MCP en /mcp,          │
 Claude Code                  │        REST en /api/tools/<Herramienta>)                │
 Power Automate (HTTP) ───────┼─▶ dev tunnel ─┘                                          │
                              └─────────────────────────────────────────────────────────┘
```

Tres formas de llegar al mismo agente:

| Desde | Cómo | Para qué sirve |
| --- | --- | --- |
| **Teams** | Bot registrado en Azure, código en tu PC a través de un túnel | Conversar: "¿por qué está rojo el pipeline #5512?" |
| **Workflows** | DevAssist avisa a un canal por un webhook de Teams Workflows; Power Automate llama a la API REST | Que el agente trabaje sin que nadie pregunte |
| **IDE** | Servidor MCP que usan GitHub Copilot (VS Code, Visual Studio) o Claude Code | Usar las herramientas del agente mientras programas |

## Coste y fechas a vigilar

| Prueba | Duración | Tarjeta | Para no pagar |
| --- | --- | --- | --- |
| Microsoft 365 Business Standard | 1 mes, hasta 25 usuarios | Sí | Desactivar la **facturación periódica** en el centro de administración antes del día 30 |
| Azure (cuenta gratuita) | 30 días o 200 $ de crédito | Sí, solo verificación | Si no pasas a pago por uso, se desactiva sola |
| Power Automate Premium (opcional) | Prueba desde make.powerautomate.com | No suele pedirla | Caduca sola |
| Copilot Studio (opcional) | Prueba, ampliable una vez | No | Caduca sola. No permite publicar |

Todo lo demás es gratis: Azure Bot en plan **F0**, el canal de Teams, los dev tunnels y Ollama en tu PC.

Apunta hoy en el calendario la fecha de fin de la prueba de Microsoft 365.

## Viernes: cuentas e instalación

Unas 2 horas. Hazlo el viernes porque **los permisos de Teams pueden tardar horas en aplicarse**.

### 1. Tenant de Microsoft 365 (Business Standard, prueba de 1 mes)

1. Ve a la página de la prueba de Microsoft 365 Business Standard y empieza con un correo cualquiera.
2. Cuando pida el dominio, acepta el `<nombre>.onmicrosoft.com`. Te crea un usuario como
   `admin@<nombre>.onmicrosoft.com`: **esa es tu cuenta de trabajo** para todo el fin de semana.
3. En el [centro de administración](https://admin.microsoft.com): **Facturación → Tus productos →
   Microsoft 365 Business Standard → desactiva la facturación periódica**. Sigue funcionando hasta el final
   de la prueba.
4. Comprueba que tienes Teams: abre [teams.microsoft.com](https://teams.microsoft.com) con esa cuenta.

### 2. Permitir apps personalizadas en Teams

En el [centro de administración de Teams](https://admin.teams.microsoft.com):

1. **Aplicaciones de Teams → Administrar aplicaciones → Acciones → Configuración de aplicaciones de toda la
   organización**: activa **Permitir que los usuarios interactúen con aplicaciones personalizadas**.
2. **Aplicaciones de Teams → Directivas de configuración → Global (predeterminado de toda la organización)**:
   activa **Cargar aplicaciones personalizadas**.

Puede tardar hasta 24 horas. El sábado lo verás en Teams: **Aplicaciones → Administrar sus aplicaciones →
Cargar una aplicación → Cargar una aplicación personalizada**.

### 3. Cuenta gratuita de Azure, con la misma cuenta

1. En [azure.microsoft.com/free](https://azure.microsoft.com/free) inicia sesión con
   `admin@<nombre>.onmicrosoft.com` (no con tu cuenta personal). Así el bot y su aplicación de Entra quedan
   en el mismo tenant que Teams.
2. En el portal: **Cost Management → Presupuestos → Agregar**, 10 € con aviso al 50 %. Por si acaso.

### 4. Herramientas

```bash
winget install Microsoft.VisualStudioCode
```

```bash
winget install Microsoft.AzureCLI
```

```bash
winget install Microsoft.devtunnel
```

En VS Code, instala las extensiones **Microsoft 365 Agents Toolkit** y **GitHub Copilot** (el plan gratuito
basta para probar el modo agente con MCP, con un número limitado de peticiones al mes).

Y DevAssist como siempre (si ya lo tienes, sáltatelo):

```bash
.venv\Scripts\pip install -e ".[dev]"
```

```bash
npm install
```

```bash
ollama pull qwen3:8b
```

```bash
ollama pull qwen3:4b
```

## Sábado mañana: DevAssist en Teams

Unas 3 horas.

### 1. Arrancarlo en local y probarlo en el Playground

Igual que en [DEVASSIST.md](DEVASSIST.md#probarlo-como-si-fuera-teams): en tres terminales,
`devassist fake-gitlab`, `devassist serve` y `npm run playground`. Si el Playground responde a
`Why is pipeline #5512 of payments-api red?`, sigue.

### 2. Túnel público hacia tu PC

Teams no puede llegar a `localhost`. Un dev tunnel da una URL `https` fija que reenvía a tu puerto 3978.

```bash
devtunnel user login
```

```bash
devtunnel create devassist --allow-anonymous
```

```bash
devtunnel port create devassist -p 3978
```

```bash
devtunnel host devassist
```

Apunta la URL que muestra, del estilo `https://abcd1234-3978.euw.devtunnels.ms`.

"Anónimo" aquí solo significa que el túnel no pide iniciar sesión, porque Bot Service no puede hacerlo.
La protección la pone DevAssist: con credenciales, rechaza cualquier petición a `/api/messages` sin un
token válido de Bot Service, y con `DEVASSIST_ALLOWED_TENANT_ID` rechaza a usuarios de otros tenants.
Mientras el túnel esté abierto, no arranques `devassist serve` sin credenciales.

### 3. Registrar el bot en Azure

En el [portal de Azure](https://portal.azure.com): **Crear un recurso → Azure Bot**.

| Campo | Valor |
| --- | --- |
| Identificador del bot | `devassist-<tu nombre>` |
| Grupo de recursos | nuevo, `rg-devassist-finde` |
| Plan de precios | **Gratis (F0)** |
| Tipo de aplicación | **Inquilino único** |
| Tipo de creación | Crear nuevo id. de aplicación de Microsoft |

Cuando termine:

1. **Configuración → Punto de conexión de mensajería**: `https://<tu túnel>/api/messages`. Guarda.
2. En la misma página, apunta el **Id. de aplicación de Microsoft** y el **Id. de inquilino de la aplicación**.
3. **Administrar contraseña** (abre la aplicación en Entra) → **Certificados y secretos → Nuevo secreto de
   cliente**. Copia el **valor** en ese momento: luego no se vuelve a mostrar.
4. **Canales → Microsoft Teams → Aplicar**.

### 4. Darle las credenciales a DevAssist

En `.env.devassist` (no se sube a git), descomenta y rellena el bloque *Real Teams from your PC*:

```
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE=ClientSecret
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=<Id. de aplicación>
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=<valor del secreto>
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=<Id. de inquilino>
DEVASSIST_ALLOWED_TENANT_ID=<Id. de inquilino>
```

Reinicia `devassist serve` y comprueba que dice `authenticated=True`. El Playground ya no funcionará con
estas variables puestas, porque no firma las peticiones: para volver a él, coméntalas.

### 5. Paquete de la app y carga en Teams

```bash
.venv\Scripts\devassist package --bot-id <Id. de aplicación> --developer "Pruebas" --website https://example.com --privacy-url https://example.com/privacy --terms-url https://example.com/terms
```

En Teams: **Aplicaciones → Administrar sus aplicaciones → Cargar una aplicación → Cargar una aplicación
personalizada** → `dist/devassist-teams.zip` → **Agregar**.

### 6. Probar

- En el chat personal con DevAssist: `Why is pipeline #5512 of payments-api red?`. Primero responde
  "On it…" y luego, cuando acaba el modelo, el diagnóstico.
- En un canal: añade la app al equipo y menciónala, `@DevAssist list my open merge requests in payments-api`.
- En la consola de `devassist serve` verás cada petición. Con `devassist usage` ves tokens y feedback.

## Sábado tarde: workflows

Unas 3 horas. Tres niveles: un webhook que publica en un canal, el agente que trabaja solo ante un evento, y
un flujo de Power Automate que llama al agente.

### 1. Webhook de Teams Workflows → canal

1. En Teams, en el canal donde quieras los avisos: **⋯ → Workflows** y elige la plantilla **Enviar alertas
   de webhook a un canal** (o *Publicar en un canal cuando se reciba una solicitud de webhook*).
2. Termina el asistente y copia la URL que te da.
3. Ponla en `.env.devassist`:

```
TEAMS_NOTIFY_WEBHOOK_URL=<URL del workflow>
```

4. Prueba que llega una tarjeta:

```bash
.venv\Scripts\devassist notify-teams
```

Workflows solo publica **Adaptive Cards** dentro de `attachments`. El formato antiguo de los conectores de
Office 365 (`{"text": "..."}`) ya no sirve, y DevAssist ya envía el nuevo.

### 2. El agente trabaja solo: pipeline fallido → diagnóstico → canal

En un GitLab real, GitLab llamaría a `/webhooks/gitlab` al fallar un pipeline. Aquí lo simula un comando
con los datos del GitLab de pruebas. En `.env.devassist`:

```
GITLAB_WEBHOOK_SECRET=<cualquier valor largo>
DEVASSIST_AUTO_TRACE_ON_PIPELINE_FAILURE=true
```

Reinicia `devassist serve` y lanza:

```bash
.venv\Scripts\devassist simulate-webhook pipeline-failed
```

Responde `HTTP 202` al momento. Cuando el agente de trazas termina, aparece en el canal una tarjeta
**DevAssist · failed pipeline · acme/payments-api** con la causa.

Otros eventos: `mr-opened` (con `DEVASSIST_AUTO_TESTS_ON_MR=true` escribe tests y los pasa por el CI de
pruebas) y `mr-merged` (con `DEVASSIST_AUTO_CHANGELOG_ON_MERGE=true` abre una MR con el changelog).

### 3. Flujos sin código (Power Automate estándar)

Incluido en Business Standard, sin pruebas extra. Ideas para practicar en
[make.powerautomate.com](https://make.powerautomate.com):

- **Programado**: cada día a las 9:00, *Publicar un mensaje en un chat o canal* con un recordatorio de la
  daily.
- **Desde un mensaje**: en Teams, **⋯** en un mensaje → *Más acciones* → crear una tarea de Planner con el
  texto del mensaje.
- **Correo**: cuando llegue un correo con "FAILED" en el asunto, publicar en el canal.

### 4. Power Automate llama a DevAssist (Premium)

La acción **HTTP** es Premium. Activa la prueba de Power Automate Premium si te la ofrece al añadirla.

1. Arranca el backend REST/MCP con clave (en otra terminal):

```bash
$env:DEVASSIST_COPILOT_API_KEY="<al menos 24 caracteres aleatorios>"; .venv\Scripts\devassist-copilot serve
```

2. Expón el puerto 3981 en el mismo túnel:

```bash
devtunnel port create devassist -p 3981
```

   y reinicia `devtunnel host devassist`. La URL de ese puerto acaba en `-3981.<región>.devtunnels.ms`.

3. Flujo programado en Power Automate:
   - **HTTP**: método `POST`, URI `https://<túnel>-3981.<región>.devtunnels.ms/api/tools/GetPipelineStatus`
   - Encabezados: `X-API-Key: <tu clave>`, `X-DevAssist-Requester: admin@<nombre>.onmicrosoft.com`,
     `Content-Type: application/json`
   - Cuerpo: `{"project": "acme/payments-api", "pipelineId": 5512}`
   - Después: **Publicar un mensaje en un chat o canal** con `status` y `url` de la respuesta (usa *Analizar
     JSON* para sacarlos).

Las 18 herramientas están en `GET /openapi.json` del mismo servidor.

## Domingo mañana: desde el IDE

Unas 2 horas. El mismo DevAssist, pero como **servidor MCP**: el asistente de tu IDE descubre sus
herramientas (pipelines, MRs, logs, tests en CI…) y las usa mientras programas.

### 1. Arrancar el servidor MCP en local

Con `devassist fake-gitlab` en marcha:

```bash
.venv\Scripts\devassist-copilot serve
```

Escucha en `http://127.0.0.1:3981/mcp`, solo en tu PC y sin clave. Si tienes puesta
`DEVASSIST_COPILOT_API_KEY` de la tarde anterior, quítala o añade el encabezado `X-API-Key` en las
configuraciones de abajo.

### 2. VS Code con GitHub Copilot

El repo ya trae [.vscode/mcp.json](../.vscode/mcp.json). Ábrelo en VS Code y pulsa **Start** encima del
servidor `devassist`. En el chat de Copilot, modo **Agent**, el icono de herramientas debe mostrar las de
DevAssist. Prueba:

> ¿Qué merge requests abiertas tiene acme/payments-api y en qué estado está el pipeline 5512?

Copilot pide permiso antes de cada herramienta que escribe (crear ramas, comentar, abrir MRs).

### 3. Visual Studio

Visual Studio 2022 (17.14 o posterior) y Visual Studio 2026 también usan servidores MCP en el modo agente de
GitHub Copilot. Según la documentación de Microsoft, leen `.mcp.json` en la carpeta de la solución y también
`.vscode/mcp.json`. Abre la carpeta del repo, entra en el chat de Copilot en modo **Agent** y comprueba que
aparecen las herramientas.

### 4. Claude Code

El repo trae [.mcp.json](../.mcp.json). Al abrir Claude Code en el repo te pide aprobar el servidor
`devassist`; con `/mcp` ves su estado y sus herramientas.

### 5. Agents Toolkit: la otra forma de llevar un agente a Teams

Para comparar con lo que hiciste el sábado a mano: en VS Code, **Microsoft 365 Agents Toolkit → Create a New
Agent/App → Custom Engine Agent** (Python) y **F5 → Debug in Teams**. El toolkit crea el túnel, registra el bot
y carga la app por ti. Fíjate en qué ficheros genera (`m365agents.yml`, `appPackage/`) y compáralo con
`devassist package` y la [plantilla de Azure](../infra/main.bicep).

## Domingo tarde: extras opcionales

Elige uno.

- **Copilot Studio (prueba)**: crea un agente y añade una herramienta **MCP** que apunte a
  `https://<túnel>-3981.<región>.devtunnels.ms/mcp` con la clave en `X-API-Key`. Solo podrás probarlo en el
  panel de pruebas, porque la prueba no permite publicar. Los ficheros del agente completo están en
  [copilot-studio/](../copilot-studio/README.md) y la explicación en
  [ALTERNATIVA-copilot-studio.md](ALTERNATIVA-copilot-studio.md).
- **Modelos de Azure con el crédito**: crea un recurso de Foundry con `gpt-5-mini`, ponlo en `.env.devassist`
  con `DEVASSIST_PROFILE=azure` y pasa la batería, que cuesta céntimos:

```bash
.venv\Scripts\devassist eval --profile azure
```

  Las suscripciones de prueba pueden tener cuota baja o nula para algunos modelos: míralo en **Foundry →
  Cuotas** antes de desplegar.
- **Microsoft 365 Copilot**: si el centro de administración te ofrece una prueba de Copilot, actívala. Se
  abren Agent Builder con tus datos, los agentes declarativos y DevAssist dentro de Copilot Chat, porque el
  paquete ya lo declara como agente de Copilot.

## Al terminar: recoger

```bash
az group delete --name rg-devassist-finde
```

```bash
devtunnel delete devassist
```

- Borra el secreto de cliente en Entra y las variables `CONNECTIONS__…` de `.env.devassist`.
- Elimina el workflow del canal si no lo vas a usar.
- Comprueba que la facturación periódica de Microsoft 365 sigue desactivada.

## Problemas típicos

| Síntoma | Causa probable | Qué mirar |
| --- | --- | --- |
| No aparece *Cargar una aplicación personalizada* | La directiva aún no se ha aplicado | Espera; revisa los dos interruptores del viernes |
| Teams no responde nada | Endpoint mal escrito o túnel caído | `https://<túnel>/healthz` en el navegador debe dar `"authenticated": true` |
| `401` en la consola de `devassist serve` | Id. de aplicación, secreto o tenant incorrectos | Las cuatro variables `CONNECTIONS__…` |
| Responde "On it…" y nada más | Falla la entrega diferida o el modelo tarda | Errores en la consola; Ollama en marcha; prueba con `--profile mock` |
| "only available to members of this organisation" | `DEVASSIST_ALLOWED_TENANT_ID` no coincide | Usa el Id. de inquilino de la app |
| `notify-teams` da error 4xx | URL del workflow incompleta o caducada | Vuelve a copiar la URL del workflow |
| `simulate-webhook` responde `ignored` | Automatización desactivada | La variable `DEVASSIST_AUTO_*` y reiniciar `serve` |
| El IDE no ve herramientas | `devassist-copilot serve` parado o con clave | `http://127.0.0.1:3981/healthz` |
