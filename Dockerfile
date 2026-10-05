FROM python:3.12-alpine
WORKDIR /app
COPY --chown=1000:1000 bridge.py dashboard.html ./
USER 1000:1000
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/healthz', timeout=3)"
CMD ["python", "-u", "bridge.py"]
