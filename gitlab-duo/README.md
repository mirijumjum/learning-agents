# DevAssist sobre GitLab Duo

Configuración para obtener los cuatro especialistas de DevAssist (tests, documentación, Git y trazas)
con la IA del propio GitLab (**GitLab Duo Agent Platform**) y llevar sus resultados a Microsoft Teams.
El análisis completo (cobertura, precios, coste, despliegue) está en
[docs/ALTERNATIVA-gitlab-duo.md](../docs/ALTERNATIVA-gitlab-duo.md).

## Qué hay aquí

| Ruta | Qué es | Dónde se usa en GitLab |
| --- | --- | --- |
| `flows/devassist-tests.yaml` | Flujo personalizado: escribe tests, los ejecuta, corrige hasta 3 veces, rama `ai/tests-<iid>` y MR | Proyecto → AI → Flows → New flow |
| `flows/devassist-docs.yaml` | Flujo: `docs/api.md`, README, changelog y docstrings, en una rama `ai/docs-*` con MR | Igual |
| `flows/devassist-trace-pipeline.yaml` | Flujo: causa probable de un pipeline fallido, con un agente lector y otro escritor separados | Igual |
| `agents/*.yaml` | Agentes personalizados para Agentic Chat (Git, trazas, tests, docs): nombre, descripción, prompt y herramientas | Proyecto → AI → Agents → New agent (se copian a mano: GitLab no los lee de ficheros) |
| `triggers.yaml` | Qué disparadores crear (mención, revisor asignado, pipeline fallido) | Proyecto → AI → Triggers |
| `project-template/` | Ficheros que se copian a la raíz de cada proyecto del piloto | Ver abajo |
| `scripts/apply_guardrails.py` | Comprueba o aplica las ramas protegidas, las reglas de aprobación y el rol de las cuentas de IA | Se ejecuta una vez por proyecto con el token de un Maintainer |
| `schemas/` | Esquemas JSON escritos a partir de la documentación oficial y la lista de herramientas documentadas | Los usan los tests |

`project-template/` contiene:

- `AGENTS.md`: instrucciones del proyecto que leen Duo Chat y los flujos.
- `.gitlab/duo/chat-rules.md`: reglas para Duo Chat.
- `.gitlab/duo/mr-review-instructions.yaml`: instrucciones para la revisión de código de Duo (Code Review Flow).
- `.gitlab/duo/agent-config.yml`: entorno de ejecución de los flujos (instalación de dependencias, caché, dominios permitidos).
- `.gitlab/duo/mcp.json`: ejemplo de servidor MCP de logs para el IDE y la CLI de Duo.
- `.gitlab/CODEOWNERS`: los ficheros de IA y de CI necesitan aprobación de un Code Owner.
- `.gitlab-ci.yml`: tests con informe JUnit y cobertura (resultado real que muestran las MRs) y el job `ai-branch-guard`.
- `.gitlab/ci/ai_branch_guard.py`: hace fallar el pipeline si una rama `ai/*` toca lo que no debe.

## El puente con Teams

```bash
devassist duo-bridge --port 3979
```

Recibe webhooks de GitLab y publica en un canal de Teams solo lo que hizo la IA: comentarios de las
cuentas de servicio de Duo, MRs de ramas `ai/*` y pipelines terminados de esas ramas. Variables:
`GITLAB_WEBHOOK_SECRET` (obligatoria), `TEAMS_NOTIFY_WEBHOOK_URL` (flujo de Workflows de Teams),
`DUO_BRIDGE_AUTHORS` y `DUO_BRIDGE_BRANCH_PREFIX`. Código: `src/devassist/duo_bridge.py`.

## Tests

```bash
python -m pytest tests/test_gitlab_duo_config.py tests/test_gitlab_duo_guard.py tests/test_gitlab_duo_bridge.py
```
