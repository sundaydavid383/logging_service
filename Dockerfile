# 1. Use an official, lightweight Python runtime
FROM python:3.11-slim

# 2. Prevent Python from writing .pyc files and buffer output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. Set the working directory inside the container
WORKDIR /app

# 4. Install basic system dependencies required for PostgreSQL binaries
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 5. Install Python dependencies
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt

# 6. Copy the rest of your Django code into the container
COPY . /app/

# 7. FIX FOR WINDOWS: Give the script execution permissions inside the container
RUN chmod +x /app/entrypoint.sh

# 8. Set the script as the gateway for the container lifecycle
ENTRYPOINT ["/app/entrypoint.sh"]