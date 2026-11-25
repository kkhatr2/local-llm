"""ASGI entry point for the local LLM API."""

from local_llm.composition import build_app

app = build_app()
