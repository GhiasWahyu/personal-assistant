FROM node:20-bookworm-slim

# Install Python & Dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    ca-certificates \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency files
COPY telegram_bot/requirements.txt /app/telegram_bot/requirements.txt
COPY whatsapp_bot/package*.json /app/whatsapp_bot/

# Install Node dependencies
WORKDIR /app/whatsapp_bot
RUN npm install --omit=dev

# Install Python dependencies
WORKDIR /app/telegram_bot
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Copy all application code
WORKDIR /app
COPY . /app/

# Set executable permission for startup script
RUN chmod +x /app/start.sh

# Expose WhatsApp Gateway Web UI
EXPOSE 3000

CMD ["/app/start.sh"]
