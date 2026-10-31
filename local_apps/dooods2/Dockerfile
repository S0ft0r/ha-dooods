# Multi-arch образ DOODS2 с моделями и ML-зависимостями закреплён по digest.
FROM docker.io/snowzach/doods2:latest@sha256:ef9632aa54ac901b4489376a5a91f68576fddd4f5f460d6994ca45b59b1f0488

WORKDIR /opt/doods

# Unicode-рендерер подписей требует Pillow; версия закреплена для повторяемой сборки.
RUN python3 -m pip install --no-cache-dir Pillow==11.3.0

# Удалить Python-код прежней версии, сохранив модели и установленные библиотеки.
RUN rm -rf /opt/doods/detectors /opt/doods/html /opt/doods/__pycache__ \
    && find /opt/doods -maxdepth 1 -type f -name '*.py' -delete

COPY application/ /opt/doods/

ENTRYPOINT ["python3", "main.py"]
CMD ["-c", "/data/options.json", "api"]
EXPOSE 8080/tcp
