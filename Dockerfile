FROM python:3.12-alpine
WORKDIR /app
COPY bridge.py .
USER 1000:1000
EXPOSE 8088
CMD ["python", "-u", "bridge.py"]
