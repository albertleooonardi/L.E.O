# Third-party software and services

L.E.O.'s source is licensed under the [MIT License](LICENSE). Its dependencies retain their own licenses.
Installing L.E.O. does not change the terms of model accounts, API services, or external tools you connect.

- **9Router** — [source](https://github.com/decolua/9router), MIT. Personal installs download the pinned npm package
  into `deploy/local-router/node_modules/`; its license ships in that package. Docker installs use the pinned upstream image.
- Python and frontend dependencies are recorded in `uv.lock` and `frontend/package-lock.json`.
  Their license notices ship with their installed distributions.
- The local router's dependency lock is `deploy/local-router/package-lock.json`; this runtime is downloaded rather
  than copied into L.E.O.'s source. Its transitive dependencies retain their notices.
- SearXNG, PostgreSQL, and container base images are separately distributed software with their own licenses.
- Screenshots may show provider names and trademarks belonging to their respective owners. Wikimedia photographs
  shown in generated pages retain their original attribution and licenses.
