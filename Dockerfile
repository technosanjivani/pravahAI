FROM python:3.11-slim


ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1


WORKDIR /app


RUN apt-get update && apt-get install -y \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*


COPY requirements.txt .


RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install gunicorn


COPY . .

# Run as a non-root user — a break-in no longer gets root inside the container
RUN addgroup --system app && adduser --system --ingroup app app \
    && chown -R app:app /app
USER app


EXPOSE 6875

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-6875} app:app"]