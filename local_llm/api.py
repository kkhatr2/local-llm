"""ASGI entry point for the local LLM API."""

from local_llm.composition import build_app, configure_logging

configure_logging()
app = build_app()
