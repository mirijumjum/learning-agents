# DevAssist en Copilot Studio

Ficheros para montar DevAssist como agente de Microsoft Copilot Studio. La explicación completa, los
precios y el despliegue están en [docs/ALTERNATIVA-copilot-studio.md](../docs/ALTERNATIVA-copilot-studio.md).

- `agent/`: el agente con la estructura de la extensión de Copilot Studio para VS Code
  (`agent.mcs.yml`, `settings.mcs.yml`, `actions/`, `topics/`, `workflows/`). Las instrucciones se editan en
  `agent/instructions.md` y se copian en `agent.mcs.yml` (un test comprueba que coinciden).
- `connector/rest/` y `connector/mcp/`: definiciones OpenAPI 2.0 de los custom connectors. Se generan desde
  el código; no las edites a mano:

```bash
.venv\Scripts\devassist-copilot export-connector --host devassist.example.com
```

El backend que llaman está en `src/devassist_copilot/` (`devassist-copilot serve`, puerto 3981).
