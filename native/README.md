# ModelMesh runtime sources

Use the [root README](../README.md) for setup and the root `uv.lock`. Run commands
from the repository root. `uv sync --frozen` installs the workspace into `.venv`.

- `src/modelmesh`: the app. It holds the API, the Temporal workflows and worker,
  the Pydantic AI agent, Auto routing and the Docker computer.
- `web`: the React application and its own assets. Shared controls live in `../frontend`.
- `skills`, `instructions.md`: the agent's bundled skills and instructions.
- `tests`: runtime, replay, routing, settings, storage and package tests.
- `hatch_build.py`: builds the web UI and includes it in the package.

See [architecture](../docs/architecture.md), [usage](../docs/usage.md) and
[verification](../docs/verification.md). `uv build --package modelmesh` builds a
wheel and a source distribution that include the built UI. It needs Node.js 22
and npm.
