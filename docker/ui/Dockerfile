FROM python:3.12-slim
WORKDIR /app
COPY ui/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt gunicorn
COPY ui /app/ui
EXPOSE 4444
CMD ["gunicorn", "--bind", "0.0.0.0:4444", "--workers", "2", "ui.app:app"]
