#!/usr/bin/env bash
# 888ai.me 站点部署到 ECS：克隆站点仓库 + 新增 nginx 虚拟主机（不动现有服务）
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if ! command -v git >/dev/null 2>&1; then
  apt-get update && apt-get install -y git
fi
if ! command -v nginx >/dev/null 2>&1; then
  apt-get update && apt-get install -y nginx
fi

mkdir -p /var/www/888ai
if [ -d /var/www/888ai/.git ]; then
  git -C /var/www/888ai pull --ff-only
else
  git clone https://github.com/zhuchong19910704-rgb/zhuchong19910704-rgb.github.io.git /var/www/888ai
fi

cat > /etc/nginx/sites-available/888ai.me <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name 888ai.me www.888ai.me;
    root /var/www/888ai;
    index index.html;
    location / { try_files $uri $uri/ =404; }
}
EOF
ln -sf /etc/nginx/sites-available/888ai.me /etc/nginx/sites-enabled/888ai.me
nginx -t
systemctl reload nginx 2>/dev/null || service nginx reload
echo SETUP_DONE
