# Alternativa: DevAssist con la API de OpenAI o de Anthropic, sin Azure AI

Precios y condiciones consultados en las páginas oficiales el **10 de noviembre de 2026**. Todos los
importes son en USD, precio de lista, pago por uso.

## Índice

- [Qué es](#qué-es)
- [Qué cubre de la guía](#qué-cubre-de-la-guía)
- [Suscripciones y cuentas necesarias](#suscripciones-y-cuentas-necesarias)
- [Coste estimado para 5 y 20 desarrolladores](#coste-estimado-para-5-y-20-desarrolladores)
- [Pros](#pros)
- [Contras](#contras)
- [Despliegue paso a paso](#despliegue-paso-a-paso)
- [Qué está verificado aquí y qué no](#qué-está-verificado-aquí-y-qué-no)

## Qué es

El mismo DevAssist de [DEVASSIST.md](DEVASSIST.md) (orquestador, agentes de tests, documentación, Git y
trazas, con las mismas protecciones), pero los modelos se llaman **directamente** en la API de OpenAI o
en la de Anthropic. No hay recurso de Foundry ni de Azure OpenAI, ni identidad administrada, Key Vault o
Container Apps. El servicio es un contenedor que corre donde quieras: en un servidor propio, junto al
GitLab de la empresa, o en cualquier nube.

Teams sigue llegando por **Azure Bot Service**, porque es la única forma de publicar un bot en Teams. El
canal de Teams es estándar y gratuito, y el recurso del bot usa el plan F0, también gratuito. Hace falta
una suscripción de Azure de pago por uso para registrarlo, pero no genera coste.

Elegir proveedor es cuestión de configuración:

| Perfil | Modelo fuerte (tests, docs, incidentes complejos) | Modelo rápido (Git, trazas, enrutado) | Variables |
| --- | --- | --- | --- |
| `openai` | `gpt-6.1-sol` | `gpt-6-luna` | `OPENAI_API_KEY`, `OPENAI_MODEL_STRONG`, `OPENAI_MODEL_FAST`, `OPENAI_BASE_URL` |
| `anthropic` | `claude-opus-5-5` | `claude-haiku-5-5` | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL_STRONG`, `ANTHROPIC_MODEL_FAST` |

Los valores por defecto siguen las recomendaciones actuales de cada proveedor: OpenAI presenta
`gpt-6.1-sol` como el modelo con mejor relación capacidad/precio y `gpt-6-luna` como el más eficiente
([modelos de OpenAI](https://developers.openai.com/api/docs/models)), y Anthropic, Claude Opus 5.5 y
Claude Haiku 5.5. Con variables de entorno se pueden usar los mismos modelos que valoró la guía
(`gpt-5` y `gpt-5-mini`, que OpenAI sigue listando y que son más baratos) o `claude-sonnet-5-5`, que
cuesta la mitad que Opus.

Qué cambia en el código (rama `alt/direct-api`):

| Fichero | Cambio |
| --- | --- |
| `src/devassist/config.py` | Perfiles `openai` y `anthropic`, con modelos, clave y URL base desde variables de entorno. Sin clave, el perfil no arranca. |
| `src/devassist/llm.py` | Crea el cliente de cada proveedor y traduce los parámetros (ver la tabla siguiente). |
| `src/devassist/audit.py` | Precios por modelo y por proveedor, incluida la caché. `devassist usage` muestra el coste real por perfil y lo que habrían costado los mismos tokens en Azure, OpenAI y Anthropic. |
| `tests/test_devassist_direct_api.py` | 13 pruebas contra servidores locales que imitan las dos APIs. |
| `.env.devassist.example`, `Dockerfile.devassist` | Ejemplos de configuración y `--build-arg EXTRAS=anthropic` para la imagen. |

Cómo se traduce cada parámetro, con los conectores de Microsoft Agent Framework
(`agent-framework-openai` 1.18.1 y `agent-framework-anthropic` 1.0.0b261106, en beta):

| Parámetro de DevAssist | OpenAI (`POST /v1/chat/completions`) | Anthropic (`POST /v1/messages`) |
| --- | --- | --- |
| Autenticación | `Authorization: Bearer <clave>` | `x-api-key: <clave>` y `anthropic-version` |
| Límite de salida (`DEVASSIST_MAX_OUTPUT_TOKENS`) | `max_completion_tokens` (los modelos con razonamiento rechazan `max_tokens`) | `max_tokens` |
| Razonamiento (`DEVASSIST_REASONING_EFFORT`, por defecto `low`) | `reasoning_effort` | `output_config.effort`. Claude Opus 5.5 y Haiku 5.5 siempre razonan (pedir `thinking: disabled` da un error 400), así que `none` se convierte en `low`. No se envía `thinking`: queda en modo adaptativo. |
| Decisión delicada ("¿falla el test o el código?") | `reasoning_effort: medium`, límite ×3 | `output_config.effort: medium`, límite ×3 |
| `temperature` | No se envía | No se envía (los modelos actuales lo rechazan) |
| Herramientas del agente Git | `tools` / `tool_calls` | `tools` / `tool_use`, con `tool_choice: auto` (forzar una herramienta da un 400 en los modelos actuales) |
| Datos en la UE | `OPENAI_BASE_URL=https://eu.api.openai.com/v1` (requiere aprobación, ver abajo) | No existe opción UE en la API de Anthropic |

## Qué cubre de la guía

Leyenda: ✅ cubierto · 🟡 parcial · ❌ no.

| Punto de la guía | OpenAI | Anthropic | Por qué |
| --- | --- | --- | --- |
| Agente de tests (escribe, CI real, hasta 3 correcciones, MR) | ✅ | ✅ | Es el mismo código. Solo cambia el modelo, y los tests siguen ejecutándose en GitLab CI. |
| Agente de documentación | ✅ | ✅ | Mismo código. |
| Agente Git (ramas, MRs, pipelines, issues) | ✅ | ✅ | Usa llamadas a herramientas, probadas contra las dos imitaciones locales. |
| Agente de trazas | ✅ | ✅ | Mismo código. Los logs pueden quedarse en Elasticsearch o JSONL; Log Analytics exigiría Azure Monitor. |
| Interfaz Teams | ✅ | ✅ | Azure Bot Service (F0, canal de Teams gratuito) con un registro de aplicación de Entra ID y secreto de cliente, porque fuera de Azure no hay identidad administrada. Necesita una URL HTTPS pública. |
| Microsoft 365 Copilot | 🟡 | 🟡 | El paquete de la app ya declara el bot como agente de Copilot y no depende del proveedor del modelo. Usarlo en Copilot Chat exige una licencia de Microsoft 365 Copilot, y el administrador tendrá que aprobar que los datos vayan a un modelo fuera de Microsoft. |
| Webhooks y automatizaciones de GitLab | ✅ | ✅ | Mismo código. |
| Garantías de seguridad (no merge/deploy/delete, enmascarado, solo el tenant) | 🟡 | 🟡 | Todas las protecciones de código siguen igual. Lo que se pierde es "sin secretos": hay que guardar y rotar la clave del proveedor, el secreto del bot y el token de GitLab en un almacén propio (Vault, secretos de Docker o Kubernetes). Además, el modelo lo procesa un tercero ajeno al contrato de Azure. |
| Datos en la UE | 🟡 | ❌ | **OpenAI**: tiene residencia en Europa (`eu.api.openai.com`, EEE y Suiza), pero hay que pedir a ventas que la activen, crear un proyecto nuevo en esa región y tener aprobado Zero Data Retention o Modified Abuse Monitoring. Cuesta un 10 % más en los modelos publicados desde el 5 de marzo de 2026. **Anthropic**: `inference_geo` solo admite `global` o `us`, y los datos en reposo de un workspace solo pueden estar en `us`. No cumple el requisito UE de la guía. |
| Auditoría y coste | ✅ | ✅ | `audit.jsonl` registra tokens, lecturas de caché y coste con el precio de cada modelo. Los avisos de presupuesto de Azure se sustituyen por los límites de gasto del proveedor (límite mensual por proyecto en OpenAI y por workspace en Anthropic), que cortan el servicio al llegar al tope en lugar de avisar. |

## Suscripciones y cuentas necesarias

| Qué | Para qué | Precio oficial | Enlace |
| --- | --- | --- | --- |
| Cuenta de organización en OpenAI Platform | Clave de API por proyecto | Pago por uso. Los niveles suben con la compra de créditos: Build (5 $ comprados, 500 $/mes como máximo), Launch (100 $, 5 000 $/mes), Grow (500 $, 200 000 $/mes) | [Límites](https://developers.openai.com/api/docs/guides/rate-limits) |
| `gpt-6.1-sol` | Modelo fuerte (OpenAI) | 2,00 $ por millón de tokens de entrada, 0,10 $ en caché, 10,00 $ de salida | [Precios OpenAI](https://developers.openai.com/api/docs/pricing) |
| `gpt-6-luna` | Modelo rápido (OpenAI) | 0,10 $ entrada, 0,01 $ en caché, 0,50 $ salida | ídem |
| `gpt-5` / `gpt-5-mini` (opcional) | Los modelos que valoró la guía | 1,25 $ / 10,00 $ y 0,25 $ / 2,00 $ | ídem |
| Batch y Flex de OpenAI | Trabajo no interactivo | 50 % de descuento en entrada y salida | ídem |
| Residencia de datos en Europa (OpenAI) | Datos procesados y guardados en la UE | +10 % en los modelos publicados desde el 5-3-2026. Requiere aprobación y ZDR o MAM | [Tus datos](https://developers.openai.com/api/docs/guides/your-data) |
| Cuenta de organización en la Claude Console (Anthropic) | Clave de API por workspace | Pago por uso, con tarjeta o factura. Niveles con tope de gasto mensual: Start (500 $), Build (1 000 $) y Scale (200 000 $) | [Límites](https://platform.claude.com/docs/en/api/rate-limits) |
| `claude-opus-5-5` | Modelo fuerte (Anthropic) | 4 $ entrada, 0,20 $ lectura de caché, 5 $ escritura de caché (5 min), 20 $ salida | [Precios Anthropic](https://platform.claude.com/docs/en/about-claude/pricing) |
| `claude-sonnet-5-5` (opcional) | Modelo fuerte más barato | 2 $ entrada, 0,10 $ lectura de caché, 10 $ salida | ídem |
| `claude-haiku-5-5` | Modelo rápido (Anthropic) | 0,10 $ entrada, 0,01 $ lectura de caché, 0,50 $ salida (prompts de hasta 100 000 tokens; DevAssist los limita a unos 12 000) | ídem |
| Batch de Anthropic | Trabajo no interactivo | 50 % de descuento, combinable con la caché | ídem |
| Azure (pago por uso) + Azure Bot Service F0 | Publicar el bot en Teams | Gratis: el canal de Teams es estándar | [Bot Service](https://azure.microsoft.com/pricing/details/bot-services/) |
| Registro de aplicación en Entra ID | Identidad del bot | Incluido en el tenant de Microsoft 365 | — |
| Microsoft 365 Copilot (opcional) | Usar DevAssist dentro de Copilot Chat | Licencia por usuario, igual que en la guía | — |
| Alojamiento del contenedor | Ejecutar DevAssist | Servidor propio: sin coste adicional. En la nube: depende del proveedor (no calculado aquí) | — |

**Retención de datos y contratos**

| | OpenAI | Anthropic |
| --- | --- | --- |
| ¿Se entrena con los datos de la API? | No, salvo que lo actives | No, sin permiso expreso |
| Retención por defecto | Registros de vigilancia de abusos durante un máximo de 30 días | Según su página, el contenido de las conversaciones no se conserva por defecto, salvo en los "Covered Models" (Fable y Mythos), que exigen 30 días |
| Zero Data Retention | Requiere aprobación de ventas | Requiere acuerdo con ventas, por organización |
| Contrato de tratamiento de datos (DPA) | [Data Processing Addendum](https://openai.com/policies/data-processing-addendum/) (enlace no comprobado hoy) | [DPA con cláusulas contractuales tipo](https://www.anthropic.com/legal/data-processing-addendum), incluido en los Commercial Terms ([ayuda](https://support.claude.com/en/articles/7996862-how-do-i-view-and-sign-your-data-processing-addendum-dpa)) |
| Fuentes | [Tus datos](https://developers.openai.com/api/docs/guides/your-data) | [Retención](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention), [Residencia](https://platform.claude.com/docs/en/manage-claude/data-residency) |

**Límites de uso al empezar** (sobra para 20 desarrolladores): Anthropic, en el nivel Start, permite
4 000 peticiones por minuto, 2 M de tokens de entrada por minuto y 400 000 de salida, tanto en Opus 5.5
como en Haiku 5.5. Las lecturas de caché no cuentan para ese límite. Las organizaciones nuevas pueden
empezar en un nivel "Evaluation", más bajo, que sube solo con el uso. El tope de 500 $/mes del nivel
Start cubre el escenario con Opus para 20 desarrolladores (unos 302 $), pero no deja mucho margen. En
OpenAI, el nivel Build tiene el mismo tope de 500 $/mes, y comprar 100 $ de crédito lo sube a 5 000 $.

## Coste estimado para 5 y 20 desarrolladores

Se usan las mismas suposiciones que la guía: 53 M de tokens de entrada y 9 M de salida al mes para 20
desarrolladores. Para que los ~140 $ de la guía cuadren con sus precios, el reparto debe ser **40 M de
entrada y 7 M de salida en el modelo fuerte y 13 M y 2 M en el rápido**. El piloto de 5 desarrolladores
es la cuarta parte. Solo se cuentan los tokens, sin caché ni batch. La guía añadía de 5 a 15 $ por
servicios de Azure, que aquí desaparecen; queda el alojamiento del contenedor.

| Proveedor y modelos | 5 desarrolladores/mes | 20 desarrolladores/mes | Por desarrollador (20) |
| --- | --- | --- | --- |
| **Guía (Azure, GPT-5 / GPT-5 mini, Data Zone UE)**, como referencia | ~35 $ + ~5 $ de servicios = **~40 $** | ~140 $ + ~15 $ = **~155 $** | ~7,75 $ |
| **OpenAI** `gpt-6.1-sol` + `gpt-6-luna` | **38,08 $** | **152,30 $** | 7,62 $ |
| OpenAI, igual pero con residencia en la UE (+10 %) | 41,88 $ | 167,53 $ | 8,38 $ |
| OpenAI `gpt-5` + `gpt-5-mini` (los modelos de la guía) | 31,81 $ | 127,25 $ | 6,36 $ |
| **Anthropic** `claude-opus-5-5` + `claude-haiku-5-5` | **75,58 $** | **302,30 $** | 15,12 $ |
| Anthropic `claude-sonnet-5-5` + `claude-haiku-5-5` | 38,08 $ | 152,30 $ | 7,62 $ |

Cómo se calcula (20 desarrolladores). OpenAI: 40 × 2 + 7 × 10 = 150 $ en el modelo fuerte, más
13 × 0,10 + 2 × 0,50 = 2,30 $ en el rápido. Anthropic con Opus: 40 × 4 + 7 × 20 = 300 $, más los
mismos 2,30 $. El test `test_guide_volume_per_vendor` comprueba estas cifras con la tabla de precios
del código.

**Qué puede mover la cifra**

- **Caché**: el modelo fuerte relee las mismas instrucciones en cada intento. Como hipótesis, si la mitad
  de la entrada del modelo fuerte saliera de caché, OpenAI bajaría a unos 114 $ al mes (20 M a 2 $ más
  20 M a 0,10 $) y Anthropic con Opus a unos 226 $ (sin contar el recargo de las escrituras en caché).
  En Anthropic, la caché solo se activa si la petición incluye `cache_control`, y **DevAssist todavía no
  lo envía**. Por eso la tabla no aplica ese descuento.
- **Tokenizador**: la guía midió en tokens de GPT. Anthropic indica que sus modelos desde la 4.7 generan
  aproximadamente un 30 % más de tokens que sus modelos anteriores con el mismo texto. Frente a GPT no
  hay medida. Con un 30 % más, Opus + Haiku rondaría los 390 $ y Sonnet + Haiku los 200 $. Para
  saberlo, hay que ejecutar `devassist eval --profile anthropic` y `devassist usage`.
- **Razonamiento**: con ambos proveedores, los tokens de razonamiento se cobran como salida. DevAssist usa
  `low` por defecto y `medium` solo en la decisión delicada.
- **Batch (−50 %)**: solo sirve para trabajo que puede esperar, como las 300 revisiones automáticas de
  MR al mes. No está implementado.

## Pros

- **Sin Azure AI**: no hay que pedir cuota de Foundry ni esperar a que un modelo llegue a la Data Zone
  UE. Los modelos nuevos están disponibles el mismo día que se publican.
- **Despliegue en cualquier sitio**: en un servidor propio junto a GitLab, el código, los logs y el
  token de GitLab no salen de la red. Solo salen los prompts, ya enmascarados.
- **Cambiar de proveedor es cambiar una variable**: `DEVASSIST_PROFILE=openai|anthropic|azure|ollama`,
  con el mismo código, los mismos tests y la misma batería de release para comparar.
- **Coste parecido al de la guía con OpenAI o con Sonnet**: unos 152 $ al mes para 20 desarrolladores
  sin servicios de Azure, frente a los ~155 $ de la guía. Con `gpt-5` y `gpt-5-mini`, unos 127 $.
- **Descuentos de caché altos**: en `gpt-6.1-sol`, Opus 5.5 y Sonnet 5.5, la entrada en caché cuesta un
  5 % del precio normal.
- **`devassist usage` compara proveedores** con los tokens reales de cualquier ejecución, incluso en
  local con Ollama, antes de pagar a nadie.
- **Límites holgados desde el principio**: el nivel Start de Anthropic ya admite 4 000 peticiones por
  minuto.

## Contras

- **Datos en la UE**: Anthropic no ofrece inferencia ni almacenamiento en la UE en su API. OpenAI sí,
  pero con aprobación de ventas, ZDR o MAM, un proyecto nuevo y un 10 % más. La guía da por hecho el
  procesamiento en la UE, así que esto es lo primero que debe validar el delegado de protección de datos.
- **Un encargado del tratamiento nuevo fuera de Microsoft**: hace falta revisar y aceptar su DPA, y el
  contrato de Azure no cubre este tráfico.
- **Secretos que gestionar**: la clave del proveedor y el secreto del bot sustituyen a la identidad
  administrada. Hay que rotarlos y guardarlos en un almacén seguro.
- **Límites de gasto que cortan el servicio**: al llegar al tope, la API devuelve errores hasta el mes
  siguiente o hasta subir el límite. En Azure, el presupuesto solo avisaba.
- **Otra factura y otra consola**, en USD, además de la de Azure (que solo tendría el bot gratuito).
- **Los nombres de los modelos cambian a menudo**: OpenAI ya va por la familia `gpt-6`. Hay que repasar
  los valores por defecto y la tabla de precios de `audit.py` cada cierto tiempo.
- **El conector de Anthropic de Agent Framework está en beta** (1.0.0b261106). Envía por su cuenta la
  cabecera `anthropic-beta: mcp-client-2025-04-04` y `anthropic-version: 2023-06-01`, y no se ha probado
  contra la API real.
- **Opus 5.5 siempre razona**: no se puede desactivar, solo bajar a `low`. En tareas sencillas, Haiku
  5.5 es lo adecuado, y es el modelo rápido por defecto.
- **Copilot Chat** sigue necesitando licencias de Microsoft 365 Copilot y la aprobación del administrador.

## Despliegue paso a paso

Requisitos: Docker, Azure CLI (solo para el bot), un nombre DNS con certificado TLS al que llegue
Internet (Bot Service tiene que poder llamar a `/api/messages`) y la cuenta bot de GitLab descrita en
[DEVASSIST.md](DEVASSIST.md#2-cuenta-bot-de-gitlab).

### 1. Validar antes de gastar

Con el perfil gratuito, mide los tokens reales de la batería de release y mira cuánto costarían en cada
proveedor:

```bash
devassist eval --profile ollama
```

```bash
devassist usage
```

El campo `estimated_cost_usd_by_vendor` da el coste en Azure, OpenAI y Anthropic.

### 2. Cuenta del proveedor

- **OpenAI**: crea un proyecto `devassist` (en la región Europa si te la aprueban), una clave de ese
  proyecto y un límite de gasto mensual para el proyecto.
- **Anthropic**: crea un workspace `devassist` en la Claude Console, una clave de ese workspace, un
  límite de gasto para el workspace y compra crédito.

### 3. Bot de Teams (Azure, gratis)

```bash
az ad app create --display-name devassist-bot --sign-in-audience AzureADMyOrg
```

```bash
az ad app credential reset --id <appId> --display-name devassist --years 1
```

```bash
az group create --name rg-devassist-bot --location westeurope
```

```bash
az bot create --resource-group rg-devassist-bot --name devassist-bot --app-type SingleTenant --appid <appId> --tenant-id <tenantId> --endpoint https://devassist.tuempresa.com/api/messages --sku F0
```

```bash
az bot msteams create --resource-group rg-devassist-bot --name devassist-bot
```

### 4. Imagen

Para OpenAI basta la imagen por defecto. Para Anthropic, añade el conector:

```bash
docker build -f Dockerfile.devassist --build-arg EXTRAS=anthropic -t devassist:1.0.0 .
```

### 5. Configuración y arranque

Crea un fichero de entorno **fuera del repositorio** (o usa los secretos de tu orquestador) con
`DEVASSIST_PROFILE`, la clave del proveedor, `GITLAB_URL`, `GITLAB_TOKEN`, `GITLAB_PROJECTS`,
`GITLAB_WEBHOOK_SECRET`, `DEVASSIST_ALLOWED_TENANT_ID` y las cuatro variables
`CONNECTIONS__SERVICE_CONNECTION__SETTINGS__*` con `AUTHTYPE=ClientSecret`. Tienes la lista completa en
`.env.devassist.example`.

```bash
docker run -d --name devassist --restart unless-stopped --env-file /etc/devassist/devassist.env -p 127.0.0.1:3978:3978 devassist:1.0.0
```

Pon delante un proxy inverso (nginx, Traefik o el ingress de tu clúster) que termine TLS en
`https://devassist.tuempresa.com` y reenvíe a `127.0.0.1:3978`. Con las credenciales del bot, el
servicio exige un token válido de Bot Service en cada mensaje.

### 6. Comprobar

```bash
docker exec devassist devassist doctor
```

```bash
docker exec devassist devassist eval
```

La batería de release contra el modelo real cuesta céntimos. Compara el resultado con el que dio Ollama.

### 7. Publicar en Teams y conectar GitLab

Igual que en los pasos 6 y 7 de [DEVASSIST.md](DEVASSIST.md#6-publicar-en-teams-y-copilot): `devassist
package --bot-id <appId> ...`, subir el zip, aprobarlo en el centro de administración y configurar los
webhooks con la URL `https://devassist.tuempresa.com/webhooks/gitlab`. Para activar una automatización,
cambia la variable en el fichero de entorno y reinicia el contenedor.

### 8. Operación

- Rota la clave del proveedor y el secreto del bot (este último caduca al año con el comando del paso 3).
- Guarda `.devassist/audit.jsonl` en un volumen y envíalo a tu sistema de logs. Application Insights es
  opcional (`APPLICATIONINSIGHTS_CONNECTION_STRING`).
- Revisa cada mes el gasto en la consola del proveedor junto con `devassist usage`.

## Qué está verificado aquí y qué no

**Verificado** (sin conexión, contra imitaciones locales de las APIs; `python -m pytest`: 110 tests en
verde, los 97 que había y 13 nuevos):

- OpenAI: `POST /v1/chat/completions`, `Authorization: Bearer`, el modelo de cada nivel,
  `max_completion_tokens` (sin `max_tokens`), `reasoning_effort` (`low`, o `medium` en la decisión
  delicada), sin `temperature`, y una llamada a herramienta de ida y vuelta.
- Anthropic: `POST /v1/messages`, `x-api-key` (sin `Authorization`), cabecera `anthropic-version`,
  `system` aparte, `max_tokens`, `output_config.effort`, sin `reasoning_effort`, `temperature` ni
  `thinking` desactivado, `tool_choice` automático, una llamada a herramienta de ida y vuelta, y que los
  bloques `thinking` se devuelven sin tocar y no se mezclan con la respuesta.
- El coste registrado en cada caso con el precio de su modelo, incluidas las lecturas de caché
  (800 000 tokens sin caché, 200 000 en caché y 100 000 de salida: 2,62 $ con `gpt-6.1-sol` y 5,24 $
  con Opus 5.5), y el desglose por proveedor de `devassist usage`, también con el perfil gratuito.
- Las variables de entorno, el error cuando falta la clave y que `none` se convierta en `low` en Anthropic.
- Los precios y condiciones de este documento, contrastados en las páginas oficiales enlazadas el
  10-11-2026, salvo el DPA de OpenAI (enlace no abierto hoy).

**No verificado**:

- Ninguna llamada real a OpenAI ni a Anthropic, porque no hay claves. No se sabe si la API real acepta
  las cabeceras que añade por su cuenta el conector beta de Anthropic, ni si los modelos `gpt-6.x`
  responden igual por Chat Completions que por la API Responses.
- La calidad de los agentes con estos modelos: la batería de release no se ha ejecutado contra ellos.
- El recuento real de tokens con el tokenizador de cada proveedor, y por tanto el coste mensual exacto.
- La caché de Anthropic (DevAssist no envía `cache_control`) y el modo batch (no implementado).
- Teams fuera de Azure con secreto de cliente, el proxy inverso y los comandos `az` y `docker` del
  despliegue. No se han ejecutado, y `docker build` con `EXTRAS=anthropic` tampoco.
- Que la empresa pueda obtener la residencia en la UE y ZDR de OpenAI: depende de una aprobación comercial.
