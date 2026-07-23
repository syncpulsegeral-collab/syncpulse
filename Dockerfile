# 1. Usar imagem oficial de Python
FROM python:3.10-slim

# 2. Definir versão do Rclone e evitar conflitos de variáveis de ambiente
ARG INSTALL_RCLONE_VER=1.74.3
ENV TZ=Europe/Lisbon
ENV PYTHONUNBUFFERED=1

# 3. Instalar dependências do sistema
RUN apt-get update && apt-get install -y -qq \
    curl \
    unzip \
    procps \
    ca-certificates \
    fuse3 \
    && rm -rf /var/lib/apt/lists/*

# 4. Instalar Rclone v1.74.3 via pacote .deb (específico para AMD64/ZimaOS)
RUN curl -fSL -o rclone.deb https://downloads.rclone.org/v${INSTALL_RCLONE_VER}/rclone-v${INSTALL_RCLONE_VER}-linux-amd64.deb \
    && dpkg -i rclone.deb \
    && rm rclone.deb

# 5. Instalar bibliotecas Python necessárias
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    watchdog \
    apscheduler \
    websockets \
	httpx

# 6. Criar estrutura de pastas
# /app_dist e /www_dist são as fontes "protegidas" dentro da imagem
# /app e /www são os pontos onde o ZimaOS vai montar os volumes
RUN mkdir -p /app /www /config /app_dist /www_dist

# 7. Copiar os ficheiros do teu PC para dentro da imagem (fontes de backup)
COPY main.py /app_dist/main.py
COPY www/ /www_dist/

# 8. Definir diretório de trabalho na pasta protegida
WORKDIR /app_dist

# 9. Expor a porta da API
EXPOSE 8181

# 10. Comando de arranque (Uvicorn mantém o processo vivo)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8181"]