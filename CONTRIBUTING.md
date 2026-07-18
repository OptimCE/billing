# Contributing to OptimCE Billing

Thank you for your interest in contributing! Issues and pull requests are
welcome from everyone. By participating in this project, you agree to abide by
our [Code of Conduct](CODE_OF_CONDUCT.md).

This repository holds the **billing service** — one of several repositories
under the [OptimCE organization](https://github.com/OptimCE). See the
[README](README.md) for what the service does and how it fits into the platform.

## Setting Up a Development Environment

The full OptimCE platform (gateway, authentication, databases, and the other
services) runs from the [monorepo](https://github.com/OptimCE/monorepo) via
Docker Compose. To work on the billing service on its own:

```bash
git clone https://github.com/OptimCE/billing.git
cd billing
python -m venv .venv
# install the development dependencies
.venv/Scripts/python.exe -m pip install -r requirements/development.txt
```

The service requires Python 3.12. Copy `.env.exemple` to `.env.local` and adjust
the values, then run the two deployables:

```bash
uvicorn main:app --reload        # billing-api
python -m worker.main            # billing-worker
```

The [README](README.md) covers the domain, the core flow, and the deployment
notes in more detail.

## Before You Open a Pull Request

Run the same checks CI runs
([`.github/workflows/`](.github/workflows) — `lint.yml`, `test.yml`, `build.yml`):

```bash
ENV=test .venv/Scripts/python.exe -m pytest -q          # full test suite (needs Docker)
.venv/Scripts/python.exe -m ruff check .                # lint
.venv/Scripts/python.exe -m mypy api ports regime utils worker
```

The test suite uses `pytest-docker` to start a throwaway PostgreSQL container, so
Docker must be available.

## Reporting Bugs and Suggesting Features

Open a [GitHub issue](https://github.com/OptimCE/billing/issues). For bugs,
include what you did, what you expected, and what happened instead — logs and
reproduction steps help a lot.

For security vulnerabilities, **do not open a public issue**; follow the
[security policy](SECURITY.md) instead.

## Submitting Pull Requests

1. Fork the repository and create a feature branch from `main`.
2. Make your changes. Keep each pull request focused on a single topic.
3. Make sure the checks above pass locally.
4. Open a pull request against `main`, describing **what** you changed and
   **why**.

Small documentation fixes are welcome as direct pull requests; for larger
changes, opening an issue first to discuss the approach can save you time.

## Commit Messages

Use short, imperative commit messages, preferably following the
[Conventional Commits](https://www.conventionalcommits.org/) style used in this
repository:

```
feat: add overdue-sweep endpoint
fix: cast crm address number to string before rendering
chore: bump fastapi
docs: document the credit-note flow
```

## License

OptimCE Billing is licensed under the [Apache License 2.0](LICENSE). By
contributing, you agree that your contributions will be licensed under the same
license.
