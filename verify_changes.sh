#!/bin/bash
set -e

echo "Checking syntax with ruff..."
ruff check src/hsai/orchestrator.py tests/test_orchestrator.py

echo "Running pytest..."
pytest tests/test_orchestrator.py::test_get_role_retrieves_explicit_roles -xvs
pytest tests/test_orchestrator.py::test_build_pr_body_includes_role -xvs
pytest tests/test_orchestrator.py::test_build_pr_body_contains_traceability -xvs

echo "All checks passed!"
