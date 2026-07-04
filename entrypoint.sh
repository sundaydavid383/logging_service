#!/bin/sh

# Exit immediately if a command exits with a non-zero status
set -e

echo "=== [FDQ Lifecycle] Waiting for database readiness... ==="
# Optional: if you have netcat or pg_isready available in your web container, 
# you can wait here. However, your docker-compose healthcheck already handles this.

echo "=== [FDQ Lifecycle] Running Database Migrations via Alembic ==="
alembic upgrade head

echo "=== [FDQ Lifecycle] Launching ${SERVICE_NAME} in [${FDQ_SERVICE_MODE}] mode ==="
# Starts your Django app using the service execution rules from your .env file
if [ "$FDQ_SERVICE_MODE" = "gateway" ]; then
    exec python manage.py runserver 0.0.0.0:8000
elif [ "$FDQ_SERVICE_MODE" = "activity" ]; then
    exec python manage.py runserver 0.0.0.0:8001
elif [ "$FDQ_SERVICE_MODE" = "error" ]; then
    exec python manage.py runserver 0.0.0.0:8002
elif [ "$FDQ_SERVICE_MODE" = "audit" ]; then
    exec python manage.py runserver 0.0.0.0:8003
elif [ "$FDQ_SERVICE_MODE" = "notification" ]; then
    exec python manage.py runserver 0.0.0.0:8004
else
    echo "ERROR: Unknown FDQ_SERVICE_MODE: ${FDQ_SERVICE_MODE}"
    exit 1
fi