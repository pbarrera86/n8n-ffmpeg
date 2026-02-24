FROM artibex/piper-http:latest

WORKDIR /app

# Copiamos entrypoint propio
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/entrypoint.sh"]
