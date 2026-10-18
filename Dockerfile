# One image for the API, the worker, migrations and the dev fakes.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src:/app
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# Dependencies come from pyproject.toml; the code runs from /app/src so the console and config paths resolve.
RUN pip install --no-cache-dir . && rm -rf build
COPY console ./console
COPY config ./config
COPY dev ./dev
COPY tests/fixtures ./tests/fixtures
RUN useradd --system --uid 10001 estate && chown -R estate /app
USER estate
EXPOSE 8080
ENTRYPOINT ["python", "-m", "estate_ops"]
CMD ["api"]
