FROM continuumio/miniconda3:24.9.2-0@sha256:39b820af05f0f9bb986ca145a0d077bbd3bd866256277dd869fefb93bd03cd21

ENV CONDA_ENV=local-llm \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_DIR=/app/models \
    CACHE_DIR=/app/.cache/responses \
    PATH=/opt/conda/envs/local-llm/bin:$PATH

WORKDIR /app

COPY environment-linux-64.lock /app/environment-linux-64.lock
RUN conda create -y --name "$CONDA_ENV" --file /app/environment-linux-64.lock \
    && conda clean -afy

COPY local_llm.yaml /app/local_llm.yaml
COPY local_llm /app/local_llm
COPY tests /app/tests
COPY docs /app/docs

RUN mkdir -p /app/models /app/.cache/responses

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1

CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
