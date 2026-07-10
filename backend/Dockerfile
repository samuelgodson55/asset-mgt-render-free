# Use an official, lightweight Python base image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Prevent Python from writing .pyc files to disk and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy requirements and install dependencies first so Docker can cache this
# layer and skip re-installing packages every time only the app code changes
COPY requirements.txt /app/

# Upgrade pip and increase the download timeout limit to handle slow network drops
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --default-timeout=100 -r requirements.txt

# Copy the rest of our application code
COPY . /app/

# SECURITY: run the app as a dedicated, unprivileged user instead of root.
# By default Docker containers run as root, which means anyone who manages
# to achieve code execution inside this container (e.g. via a future
# dependency vulnerability) would have full root privileges within it.
# Creating and switching to a plain user drastically reduces the blast
# radius of that scenario, at essentially zero cost for this app (it
# doesn't need to bind to a privileged port or write outside /app).
RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

# Run the FastAPI server with live-reload enabled for development.
# For a PRODUCTION deploy: drop "--reload" (it re-imports your whole app on
# every file change -- pure overhead once you're not actively editing code
# and, on some platforms, can even trigger from filesystem noise other than
# your own edits), and run behind multiple workers instead, e.g.:
#   CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
# See README.md's "Running in Production" section for the full checklist
# (ENVIRONMENT=production, a real JWT_SECRET_KEY, AUTO_INIT_DB/
# AUTO_SEED_DEMO_DATA=false, a reverse proxy terminating HTTPS, etc.)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
