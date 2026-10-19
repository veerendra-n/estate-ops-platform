# One image for the API (with the built cockpit), the worker, migrations and the estate simulator.

FROM node:22-alpine AS cockpit
WORKDIR /cockpit
COPY cockpit/package.json cockpit/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY cockpit ./
RUN npm run build

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf build
COPY config ./config
COPY skills ./skills
COPY --from=cockpit /cockpit/dist ./cockpit/dist
RUN useradd --system --uid 10001 estate && chown -R estate /app
USER estate
EXPOSE 8080
ENTRYPOINT ["python", "-m", "estate_ops"]
CMD ["api"]
