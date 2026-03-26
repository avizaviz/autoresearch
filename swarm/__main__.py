"""Entry point: python -m swarm — prints usage."""
import sys

print("Usage:")
print("  uv run python -m swarm.orchestrator --host 0.0.0.0 --port 8765")
print("  uv run python -m swarm.worker --server http://HOST:8765 --repo /path/to/repo")
sys.exit(0)
