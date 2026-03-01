FROM python:3.11-slim

# System dependencies required by Playwright/Chromium
RUN apt-get update && apt-get install -y \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium and its OS-level dependencies
RUN playwright install chromium --with-deps

COPY . .

EXPOSE 5050

CMD ["python", "app.py"]
