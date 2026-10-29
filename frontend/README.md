# L.E.O. frontend

Astro + TypeScript interface for the L.E.O. backend. See the [root README](../README.md) for setup, providers, orchestration, and deployment.

```sh
npm ci
npm run dev
```

The dev server runs on `http://127.0.0.1:4321` and proxies `/api` to the backend on port 8000. Run `make dev` from the project root in another terminal.

```sh
npm run check
npm run build
```

The production build goes to `dist/`, which FastAPI serves on port 8000 after startup. Rebuild and restart the backend if the build directory did not exist at startup.

Features: local/cloud provider settings, model catalog checks, role and budget controls, Quick/Reviewed/Deep modes, streaming responses, durable server history, cancellation, event replay, run usage/activity, safe code-block rendering, copy, conversation search, and dark/light themes.

API keys are submitted to the backend, encrypted there, and never returned to the browser. Only theme and the selected conversation ID use localStorage. Prior scripted demo conversations are not imported into real server conversations; any old `leo-workspace` browser data remains separate.
