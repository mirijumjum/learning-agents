# Alternativas a Foundry para DevAssist

La propuesta de la guía ([Teams-AI-Agents-Microsoft-Foundry.md](../Teams-AI-Agents-Microsoft-Foundry.md))
y su implementación en `main` ([DEVASSIST.md](DEVASSIST.md)) usan **Microsoft Foundry con Azure OpenAI**.
Aquí se comparan seis formas distintas de conseguir lo mismo. Cada una está construida en su propia rama,
con código o configuración y un documento detallado.

Precios consultados el 10/11/2026 en las páginas oficiales enlazadas en cada documento. Los costes
mensuales son estimaciones con la carga de trabajo de la guía: unos 53 M de tokens de entrada y 9 M de
salida al mes para 20 desarrolladores.

> ⚠️ **Nada de esto se ha probado en un tenant, una suscripción o una plataforma de pago reales.** Lo
> verificado es local: tests automáticos, validación de manifiestos y esquemas, y simulaciones de GitLab y
> de las APIs. Las cifras de coste de las opciones por créditos (Copilot Studio, GitHub Copilot, GitLab Duo)
> dependen de supuestos de consumo que no se han medido.

## Índice

- [Resumen](#resumen)
- [Qué cubre cada opción](#qué-cubre-cada-opción)
- [Coste mensual estimado](#coste-mensual-estimado)
- [Las opciones una a una](#las-opciones-una-a-una)
- [Cuál elegir](#cuál-elegir)
- [Estado de verificación](#estado-de-verificación)

## Resumen

| Opción | Rama | En una frase | Suscripción principal | 20 devs/mes |
| --- | --- | --- | --- | --- |
| **Foundry (referencia)** | `main` | Agentes propios en un contenedor, modelos GPT-5 en Azure (UE) | Azure, pago por uso | ~155 $ |
| **Copilot Studio** | [`alt/copilot-studio`](https://github.com/mirijumjum/learning-agents/tree/alt/copilot-studio) | Agente low-code de Power Platform con las herramientas en un backend propio (MCP/REST) | Créditos de Copilot Studio | ~246 $ (~125 $ si ya tienen M365 Copilot) |
| **Microsoft 365 Copilot ("full Copilot")** | [`alt/m365-copilot`](https://github.com/mirijumjum/learning-agents/tree/alt/m365-copilot) | Agente declarativo: el modelo y el orquestador son los de Copilot; nosotros solo damos las acciones | Ninguna obligatoria (*sin verificar*), o M365 Copilot | ~10 $ sin licencias · ~610 $ con licencia de empresa |
| **GitHub Copilot** | [`alt/github-copilot`](https://github.com/mirijumjum/learning-agents/tree/alt/github-copilot) | Copilot coding agent, custom agents y code review, configurados en el repositorio | Copilot Business + GitHub Team | ~630–1.005 $ |
| **GitLab Duo** | [`alt/gitlab-duo`](https://github.com/mirijumjum/learning-agents/tree/alt/gitlab-duo) | Flujos y agentes de la Duo Agent Platform dentro de GitLab, con avisos a Teams | GitLab Premium + GitLab Credits | ~1.323 $ (Haiku) a ~3.554 $ (Sonnet) |
| **Modelos propios** | [`alt/self-hosted`](https://github.com/mirijumjum/learning-agents/tree/alt/self-hosted) | El mismo DevAssist con modelos abiertos en una GPU propia (vLLM/Ollama) | Ninguna (cuenta gratuita de Azure para el bot) | ~214–999 €, coste fijo |
| **API directa** | [`alt/direct-api`](https://github.com/mirijumjum/learning-agents/tree/alt/direct-api) | El mismo DevAssist llamando a OpenAI o Anthropic sin Azure | Cuenta de API de OpenAI o Anthropic | ~127–302 $ |

## Qué cubre cada opción

✅ cubierto · 🟡 parcial · ❌ no

| Punto de la guía | Foundry | Copilot Studio | M365 Copilot | GitHub Copilot | GitLab Duo | Modelos propios | API directa |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Agente de tests (bucle fijo, CI real) | ✅ | 🟡 el orden lo decide el modelo | 🟡 asíncrono, el usuario pregunta | 🟡 límites por instrucción, no por código | 🟡 el CI llega cuando el flujo ya acabó | ✅ | ✅ |
| Documentación | ✅ | ✅ | ✅ | 🟡 sin changelog automático | 🟡 sin changelog automático | ✅ | ✅ |
| Git (ramas, MRs, issues) | ✅ | ✅ | ✅ | 🟡 solo lectura | ✅ en GitLab/IDE | ✅ | ✅ |
| Trazas (logs, pipelines) | ✅ | 🟡 stack traces sin enmascarar | ✅ | 🟡 sin logs de producción | 🟡 logs de app vía MCP beta | ✅ | ✅ |
| Teams (chat y canales) | ✅ | ✅ | 🟡 solo chat de Copilot | 🟡 `@GitHub`, con licencia por usuario | 🟡 solo avisos | ✅ | ✅ |
| Microsoft 365 Copilot | 🟡 con licencia | 🟡 | ✅ nativo | ❌ | ❌ | 🟡 con licencia | 🟡 con licencia |
| Webhooks y automatizaciones | ✅ | ❌ | ❌ | 🟡 | ✅ nativos | ✅ | ✅ |
| Nunca fusionar ni borrar | ✅ en código | ✅ en código | ✅ en código + confirmación | ✅ lo impone GitHub | ✅ lo impone GitLab | ✅ en código | ✅ en código |
| Enmascarado de datos personales | ✅ | 🟡 | ✅ | 🟡 | ❌ solo secretos | ✅ | ✅ |
| Datos en la UE | ✅ Data Zone EU | 🟡 sin modelos de razonamiento | 🟡 lo opera Microsoft | 🟡 GHE.com, +10 % | ❌ en GitLab.com | ✅ nada sale de casa (Teams sí pasa por Microsoft) | 🟡 OpenAI con aprobación, +10 % · ❌ Anthropic |
| Auditoría y coste por petición | ✅ | 🟡 créditos en Power Platform | 🟡 sin recuento de tokens | ✅ presupuestos de GitHub | 🟡 sin coste por petición | ✅ | ✅ por proveedor |
| Se puede probar con la batería de release | ✅ | 🟡 solo el backend | ❌ el modelo es de Copilot | 🟡 solo la configuración | 🟡 solo la configuración | ✅ | ✅ |

## Coste mensual estimado

| Opción | 5 devs | 20 devs | De qué depende |
| --- | --- | --- | --- |
| **Foundry (referencia)** | ~40 $ | ~155 $ | Tokens de GPT-5 / GPT-5 mini. Más un contenedor siempre encendido y el registro de contenedores, que la guía no contaba |
| **Copilot Studio** | ~75 $ | ~246 $ | 0,01 $ por crédito, o un paquete de 200 $ por 25.000 créditos. Con un modelo de razonamiento sumaría ~6.200 $ |
| **M365 Copilot** sin licencias | ~10 $ | ~10 $ | Solo el hospedaje de la API. Según la documentación, un agente con instrucciones y acciones no se mide en Copilot Chat. **Hay que confirmarlo en un tenant** |
| **M365 Copilot** con licencias | ~100–160 $ | ~370–610 $ | 21 $ (Business) o 30 $ (empresa) por usuario |
| **GitHub Copilot** | ~150–245 $ | ~630–1.005 $ | 19 $ por usuario más créditos por sesión del agente (supuesto: ~1 $). Exige GitHub |
| **GitLab Duo** (si ya tienen Premium) | ~331–888 $ | ~1.323–3.554 $ | Créditos de 1 $. El modelo por defecto (Sonnet 5.5) da 3,2 llamadas por crédito. Llamadas por tarea supuestas |
| **Modelos propios** | ~214 € | ~214–999 € | Alquiler de GPU (Hetzner GEX45 o GEX63) o compra amortizada. Fijo, no sube con el uso |
| **API directa**, OpenAI `gpt-5` | ~32 $ | ~127 $ | Tokens. Residencia en la UE: +10 % y aprobación comercial |
| **API directa**, OpenAI `gpt-6.1-sol` / `gpt-6-luna` | ~38 $ | ~152 $ | Tokens |
| **API directa**, Anthropic Sonnet 5.5 + Haiku 5.5 | ~38 $ | ~152 $ | Tokens. Sin opción de UE |
| **API directa**, Anthropic Opus 5.5 + Haiku 5.5 | ~76 $ | ~302 $ | Tokens |

En la API directa y en los modelos propios hay que sumar el hospedaje del contenedor. En las opciones de
Microsoft, Teams y Bot Service son gratis.

## Las opciones una a una

### Copilot Studio (low-code)

Documento: [ALTERNATIVA-copilot-studio.md](https://github.com/mirijumjum/learning-agents/blob/alt/copilot-studio/docs/ALTERNATIVA-copilot-studio.md)

**Qué hay en la rama:**
- Ficheros del agente de Copilot Studio (`.mcs.yml`).
- Un flujo de agente que espera al CI.
- Un backend MCP + REST que reutiliza las garantías de `src/devassist`.
- Dos conectores Swagger 2.0.
- 30 tests nuevos.

**Suscripciones:**
- Créditos de Copilot Studio: pago por uso a 0,01 $ por crédito, o paquete de 200 $ al mes por 25.000 créditos.
- Los creadores necesitan la licencia de usuario de Copilot Studio, que es gratuita pero exige tener el paquete. La de prueba no permite publicar.
- Con Microsoft 365 Copilot (30 $/usuario) el uso interactivo no gasta créditos.

**Pros:**
- No hay bot que mantener ni modelos que desplegar.
- Publicar en Teams y Copilot es activar un canal.
- Gobierno de Power Platform: DLP, límites y analítica.

**Contras:**
- Más caro y menos predecible que Foundry.
- Sigue haciendo falta un backend propio.
- Los modelos de razonamiento son carísimos y salen de la UE.
- Sin webhooks.
- La plataforma cambia a menudo.

### Microsoft 365 Copilot ("full Copilot")

Documento: [ALTERNATIVA-m365-copilot.md](https://github.com/mirijumjum/learning-agents/blob/alt/m365-copilot/docs/ALTERNATIVA-m365-copilot.md)

**Qué hay en la rama:**
- Proyecto de Agents Toolkit con el agente declarativo (v1.9), el plugin (v2.4) y el OpenAPI, validados contra los esquemas oficiales.
- Una API de acciones con el bucle de tests como trabajo asíncrono, que se arranca y luego se consulta su estado.
- Autenticación por Entra SSO.

**Suscripciones:**
- Ninguna obligatoria según la documentación (*sin verificar*).
- Microsoft 365 Copilot: 30 $/usuario (empresa) o 21 $ (Business, 18 $ en promoción).

**Pros:**
- Sin modelos ni tokens que pagar.
- Infraestructura mínima.
- Integración nativa en Copilot, con confirmación antes de cada escritura.
- Mejora sola cuando mejora Copilot.

**Contras:**
- No se elige el modelo ni se puede pasar la batería de release.
- Sin webhooks ni avisos proactivos.
- No funciona en canales de Teams.
- Límites de la plataforma: 8.000 caracteres de instrucciones, OpenAPI sin objetos anidados.
- Si se compran licencias solo para esto, cuesta entre 2,5 y 4 veces Foundry.

### GitHub Copilot

Documento: [ALTERNATIVA-github-copilot.md](https://github.com/mirijumjum/learning-agents/blob/alt/github-copilot/docs/ALTERNATIVA-github-copilot.md)

**Qué hay en la rama:**
- `.github/copilot-instructions.md`, instrucciones por ruta y cuatro custom agents con las mismas barreras.
- `copilot-setup-steps.yml` y CI.
- Un workflow de diagnóstico apagado por defecto.
- Un ruleset en JSON.
- 24 tests que comprueban los formatos documentados.

**Suscripciones:**
- Copilot Business: 19 $/usuario, con 1.900 créditos.
- Copilot Enterprise: 39 $/usuario, que exige GitHub Enterprise Cloud (21 $/usuario).
- GitHub Team: 4 $/usuario.
- Minutos de Actions.
- Residencia en la UE: +10 %.

**Pros:**
- Sin infraestructura propia.
- GitHub impone que el agente no fusione.
- Configuración versionada, que también sirve en VS Code.
- Autocompletado en el IDE.

**Contras:**
- **La empresa usa GitLab:** hay que migrar, hacer un espejo de un solo sentido o quedarse solo con el IDE.
- Licencia por usuario incluso desde Teams.
- Los límites del bucle de tests son instrucciones, no código.
- Sin acceso a logs de producción ni Microsoft 365 Copilot.

### GitLab Duo

Documento: [ALTERNATIVA-gitlab-duo.md](https://github.com/mirijumjum/learning-agents/blob/alt/gitlab-duo/docs/ALTERNATIVA-gitlab-duo.md)

**Qué hay en la rama:**
- Flujos y agentes de la Duo Agent Platform.
- Un job de CI `ai-branch-guard` y la CI validada contra el esquema oficial de GitLab.
- Un script que aplica las protecciones de ramas.
- Un puente a Teams (`devassist duo-bridge`).
- 46 tests nuevos.

**Suscripciones:**
- GitLab Premium: 29 $/usuario. Ultimate, bajo consulta.
- GitLab Credits: 1 $ por crédito. Premium incluye 12 $ por usuario y mes en una promoción temporal, que solo aplica a GitLab.com.

**Pros:**
- Funciona donde ya trabajan (MRs, IDE).
- Disparadores nativos: mención, MR creada, pipeline fallido.
- Revisión de código y Fix CI incluidos.
- Las barreras las impone GitLab.

**Contras:**
- La más cara: entre 8 y 23 veces Foundry.
- Teams solo recibe avisos, y no hay Copilot.
- Sin datos en la UE en GitLab.com.
- No enmascara datos personales.
- Los agentes y disparadores se configuran en la interfaz.
- La plataforma cambia rápido.

### Modelos propios (self-hosted)

Documento: [ALTERNATIVA-self-hosted.md](https://github.com/mirijumjum/learning-agents/blob/alt/self-hosted/docs/ALTERNATIVA-self-hosted.md)

**Qué hay en la rama:**
- Un perfil genérico `openai-compatible`, que funciona con vLLM, Ollama o cualquier servidor compatible.
- `deploy/self-hosted/` con Docker Compose: el modelo, DevAssist y Caddy con TLS.
- 15 tests nuevos.

**Suscripciones y hardware:**
- Ninguna de pago. El bot de Teams necesita una suscripción gratuita de Azure, o un bot gestionado por Teams sin suscripción (por probar).
- Modelos gratuitos: gpt-oss y Qwen3, con licencias Apache 2.0 o MIT.
- GPU: alquiler de 214 €/mes (24 GB) o 999 €/mes (96 GB), o compra amortizada.

**Pros:**
- Ningún prompt, diff ni log sale a una nube de IA.
- Coste fijo.
- Versiones del modelo fijadas.
- Mismo código que `main`.

**Contras:**
- No ahorra dinero con este volumen de uso; su valor es la soberanía de los datos.
- Los modelos pequeños son menos fiables y cada uno necesita su ajuste.
- Operación propia: drivers, SLA y un único punto de fallo.
- Hay que abrir el puerto 443.

### API directa (OpenAI o Anthropic)

Documento: [ALTERNATIVA-api-directa.md](https://github.com/mirijumjum/learning-agents/blob/alt/direct-api/docs/ALTERNATIVA-api-directa.md)

**Qué hay en la rama:**
- Perfiles `openai` y `anthropic`, cada uno con sus parámetros correctos: `max_completion_tokens` y `reasoning_effort` en OpenAI, `output_config.effort` en Anthropic.
- El precio por proveedor en la auditoría.
- 13 tests contra sustitutos locales de ambas APIs.

**Suscripciones:**
- Cuenta de API de OpenAI o de Anthropic, más el DPA de cada proveedor.
- El bot va en Azure Bot Service F0, que es gratis.

**Pros:**
- Coste parecido a Foundry con `gpt-5` o con Sonnet.
- Modelos nuevos el mismo día.
- Se despliega en cualquier sitio.
- Cambiar de proveedor es cambiar una variable.

**Contras:**
- Anthropic no tiene opción de UE; la de OpenAI exige aprobación y cuesta un 10 % más.
- Un encargado del tratamiento nuevo.
- Secretos que gestionar, sin identidad administrada.
- Los límites de gasto cortan el servicio en vez de avisar.

## Cuál elegir

- **Datos en la UE, cobertura completa y menor coste por uso:** seguir con **Foundry** (`main`).
- **Ya pagan Microsoft 365 Copilot, o se confirma que el agente declarativo no se mide:**
  **M365 Copilot** es la opción más barata. Pero no hay automatizaciones y no se elige el modelo.
  **Merece una prueba en un tenant antes de decidir.**
- **Prefieren no mantener infraestructura y valoran el gobierno de Power Platform:** **Copilot Studio**,
  aceptando más coste y menos control.
- **Los datos no pueden salir de la empresa:** **modelos propios**, aceptando más operación y una
  calidad menor con modelos pequeños.
- **Quieren el mejor modelo del momento y la UE no es un requisito estricto:** **API directa**.
- **Planean pasarse a GitHub:** **GitHub Copilot**. Con GitLab apenas funciona.
- **Quieren todo dentro de GitLab y Teams les basta para avisos:** **GitLab Duo**. Es la opción más cara
  y sin residencia de datos en la UE en GitLab.com.

Las ramas que reutilizan el código de DevAssist (modelos propios y API directa) se pueden combinar con
`main`: son perfiles nuevos, no sustituyen nada.

## Estado de verificación

| Rama | Tests (pytest) | Qué se probó | Qué no |
| --- | --- | --- | --- |
| `alt/copilot-studio` | 127 ✅ | Backend MCP/REST en vivo contra el GitLab de pruebas. Diagnóstico de un pipeline con qwen3:8b vía MCP | Copilot Studio real, importar los ficheros, créditos reales, el bucle completo con un modelo real |
| `alt/m365-copilot` | 110 ✅ | Bucle de tests por HTTP con CI real. Manifiestos validados contra los esquemas oficiales | Tenant, Copilot real, que no se mida sin licencia, límites de los plugins |
| `alt/github-copilot` | 121 ✅ · batería mock 7/7 | Formatos de configuración frente a la documentación de GitHub | Ninguna ejecución en GitHub, coste por sesión del agente |
| `alt/gitlab-duo` | 143 ✅ | CI frente al esquema oficial, job de cobertura, guardas y puente | Que GitLab acepte los flujos, disparadores reales, Teams, consumo de créditos |
| `alt/self-hosted` | 112 ✅ · batería real 5/7 | Perfil genérico contra Ollama. Los 2 fallos fueron límites de tiempo por la GPU compartida con otras sesiones | Docker, vLLM, gpt-oss, Teams, carga con 20 usuarios |
| `alt/direct-api` | 110 ✅ | Forma de las peticiones y cálculo del coste contra sustitutos locales de las dos APIs | Llamadas reales, calidad de los agentes con esos modelos, residencia en la UE |
