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

# 7. Expose the port your application will run on
EXPOSE 8000

# 8. Start the Django development server
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]