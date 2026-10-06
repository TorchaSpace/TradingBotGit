FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml requirements.txt ./
COPY src ./src
RUN pip install --no-cache-dir .
# .env, state/, logs/ and data/ are mounted from the host (see docker-compose.yml)
ENTRYPOINT ["python", "-m", "tradingbot"]
CMD ["run"]
