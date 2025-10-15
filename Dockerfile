# Start from a lightweight Python image
FROM python:3.10-slim

# Prevent Python from buffering stdout (for live logs)
ENV PYTHONUNBUFFERED=1

# Install Chromium and ChromeDriver
RUN apt-get update && \
    apt-get install -y ca-certificates && \
    update-ca-certificates && \
    apt-get install -y chromium chromium-driver wget gnupg unzip fonts-liberation \
        libnss3 libxss1 libappindicator3-1 libasound2 libatk-bridge2.0-0 libgtk-3-0 && \
    rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your scraper
COPY scraper.py .

# Set Chromium binary path for Selenium
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

# ✅ Run directly as the container entrypoint (preserves Kubernetes env vars)
ENTRYPOINT ["python", "scraper.py"]
