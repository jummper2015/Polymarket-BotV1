#!/bin/bash

# ============================================================
# Script para configurar Nginx + SSL
# Ejecutar como ROOT
# ============================================================

if [ "$EUID" -ne 0 ]; then
    echo "❌ Este script debe ejecutarse como root"
    echo "   Usar: sudo bash scripts/setup_nginx.sh tudominio.com"
    exit 1
fi

# Colores
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Verificar argumento
if [ -z "$1" ]; then
    echo -e "${RED}❌ Error: Falta el dominio${NC}"
    echo "   Uso: sudo bash scripts/setup_nginx.sh polymarket.tudominio.com"
    exit 1
fi

DOMAIN=$1

echo "🌐 Configurando Nginx para: $DOMAIN"
echo ""

# ============================================================
# 1. Verificar que nginx está instalado
# ============================================================
if ! command -v nginx &> /dev/null; then
    echo "📦 Nginx no está instalado. Instalando..."
    apt update
    apt install -y nginx
    echo -e "${GREEN}✅ Nginx instalado${NC}"
else
    echo -e "${GREEN}✅ Nginx ya está instalado${NC}"
fi

# ============================================================
# 2. Crear configuración de Nginx
# ============================================================
NGINX_CONFIG="/etc/nginx/sites-available/polymarket"

echo ""
echo "📝 Creando configuración de Nginx..."

cat > "$NGINX_CONFIG" << EOF
server {
    listen 80;
    server_name $DOMAIN;

    # Logs
    access_log /var/log/nginx/polymarket_access.log;
    error_log /var/log/nginx/polymarket_error.log;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;

        # Headers
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # Timeouts para WebSocket
        proxy_connect_timeout 600s;
        proxy_send_timeout 600s;
        proxy_read_timeout 86400s;

        # Cache
        proxy_cache_bypass \$http_upgrade;
    }

    # Health check endpoint (sin autenticación para monitoring)
    location /healthz {
        proxy_pass http://127.0.0.1:5000/healthz;
        access_log off;
    }
}
EOF

echo -e "${GREEN}✅ Configuración creada: $NGINX_CONFIG${NC}"

# ============================================================
# 3. Activar el sitio
# ============================================================
echo ""
echo "🔗 Activando sitio..."

# Eliminar default si existe
if [ -L "/etc/nginx/sites-enabled/default" ]; then
    rm /etc/nginx/sites-enabled/default
    echo "   Removido sitio default"
fi

# Crear symlink
ln -sf "$NGINX_CONFIG" /etc/nginx/sites-enabled/polymarket
echo -e "${GREEN}✅ Sitio activado${NC}"

# ============================================================
# 4. Probar configuración
# ============================================================
echo ""
echo "🧪 Probando configuración de Nginx..."
if nginx -t; then
    echo -e "${GREEN}✅ Configuración válida${NC}"
else
    echo -e "${RED}❌ Error en la configuración de Nginx${NC}"
    exit 1
fi

# ============================================================
# 5. Recargar Nginx
# ============================================================
echo ""
echo "🔄 Recargando Nginx..."
systemctl reload nginx
echo -e "${GREEN}✅ Nginx recargado${NC}"

# ============================================================
# 6. Verificar que Nginx está corriendo
# ============================================================
if systemctl is-active --quiet nginx; then
    echo -e "${GREEN}✅ Nginx está corriendo${NC}"
else
    echo -e "${YELLOW}⚠️  Arrancando Nginx...${NC}"
    systemctl start nginx
fi

# ============================================================
# 7. Configurar SSL con Let's Encrypt
# ============================================================
echo ""
echo "🔒 Configurando SSL con Let's Encrypt..."
echo ""

# Verificar que certbot está instalado
if ! command -v certbot &> /dev/null; then
    echo "📦 Instalando certbot..."
    apt install -y certbot python3-certbot-nginx
fi

echo -e "${YELLOW}════════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}IMPORTANTE: Verifica que el dominio $DOMAIN apunta a este servidor${NC}"
echo -e "${YELLOW}════════════════════════════════════════════════════════${NC}"
echo ""
read -p "¿El dominio ya apunta a este servidor? (y/n): " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    echo "📧 Certbot necesita un email para notificaciones..."
    read -p "Ingresa tu email: " EMAIL

    echo ""
    echo "🔐 Obteniendo certificado SSL..."
    certbot --nginx -d "$DOMAIN" --email "$EMAIL" --agree-tos --no-eff-email --redirect

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✅ SSL configurado correctamente${NC}"
        echo ""
        echo "🔄 Auto-renovación configurada (certbot timer)"
        systemctl status certbot.timer --no-pager
    else
        echo -e "${RED}❌ Error al obtener certificado SSL${NC}"
        echo "   Verifica que el dominio apunte correctamente a este servidor"
    fi
else
    echo -e "${YELLOW}⚠️  SSL no configurado${NC}"
    echo "   Configura el DNS y luego ejecuta:"
    echo "   sudo certbot --nginx -d $DOMAIN"
fi

# ============================================================
# 8. Resumen final
# ============================================================
echo ""
echo "════════════════════════════════════════════════════════"
echo -e "${GREEN}✅ Nginx configurado correctamente${NC}"
echo "════════════════════════════════════════════════════════"
echo ""
echo "🌐 Accede al dashboard en:"
if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    echo "   https://$DOMAIN"
else
    echo "   http://$DOMAIN (sin SSL todavía)"
fi
echo ""
echo "📝 Archivos importantes:"
echo "   Config: $NGINX_CONFIG"
echo "   Logs:   /var/log/nginx/polymarket_*.log"
echo ""
echo "🔧 Comandos útiles:"
echo "   Recargar:  systemctl reload nginx"
echo "   Logs:      tail -f /var/log/nginx/polymarket_error.log"
echo "   SSL:       certbot certificates"
echo ""
