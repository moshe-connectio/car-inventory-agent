#!/bin/bash
# setup.sh - הרץ פעם אחת על ה-Droplet
set -e

echo "=== Car Agent Setup ==="

apt update && apt install -y python3 python3-venv redis-server nginx

systemctl enable redis-server && systemctl start redis-server

cd /opt/car-agent
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn celery redis httpx openai json-repair

# .env - סודות (לא ב-git). צור מ-.env.example לפני ההפעלה הראשונה.
if [ ! -f /opt/car-agent/.env ]; then
  echo "⚠️  /opt/car-agent/.env חסר — העתק מ-.env.example ומלא ערכים אמיתיים:"
  echo "    cp /opt/car-agent/.env.example /opt/car-agent/.env && nano /opt/car-agent/.env"
  cp /opt/car-agent/.env.example /opt/car-agent/.env
fi
chmod 600 /opt/car-agent/.env

# systemd - Webhook
cat > /etc/systemd/system/car-webhook.service << 'EOF'
[Unit]
Description=Car Agent Webhook
After=network.target redis-server.service
[Service]
Environment=PYTHONPATH=/opt/car-agent
EnvironmentFile=/opt/car-agent/.env
WorkingDirectory=/opt/car-agent
ExecStart=/opt/car-agent/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

# systemd - Worker
cat > /etc/systemd/system/car-worker.service << 'EOF'
[Unit]
Description=Car Agent Celery Worker
After=network.target redis-server.service
[Service]
Environment=PYTHONPATH=/opt/car-agent
EnvironmentFile=/opt/car-agent/.env
WorkingDirectory=/opt/car-agent
ExecStart=/opt/car-agent/venv/bin/celery -A tasks worker --loglevel=info --concurrency=1 --max-tasks-per-child=50
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

# systemd - Beat (scheduler)
cat > /etc/systemd/system/car-beat.service << 'EOF'
[Unit]
Description=Car Agent Celery Beat
After=network.target redis-server.service
[Service]
WorkingDirectory=/opt/car-agent
ExecStart=/opt/car-agent/venv/bin/celery -A tasks beat --loglevel=info
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

# Nginx proxy
cat > /etc/nginx/sites-available/car-agent << 'EOF'
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF

ln -sf /etc/nginx/sites-available/car-agent /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

systemctl daemon-reload
systemctl enable car-webhook car-worker car-beat
systemctl start car-webhook car-worker car-beat

echo ""
echo "✅ הכל עלה!"
echo ""
echo "בדיקות:"
echo "  curl http://localhost/health"
echo "  curl http://localhost/run-now"
echo "  journalctl -u car-worker -f"
