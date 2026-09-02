FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY app ./app

RUN useradd --create-home --uid 10001 gitspy
USER gitspy

CMD ["python", "-m", "app.main"]
