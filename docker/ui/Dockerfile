FROM python:3.15-slim
WORKDIR /app
COPY ui/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt gunicorn
# The UI creates the tables from the shared schema if nothing else has yet (MySQL only).
COPY src/schema.sql /app/src/schema.sql
COPY ui /app/ui
# Needs no root: it only reads its files and talks to MySQL (and, with single sign-on, to the identity provider
# over HTTPS). gunicorn keeps a control socket in $HOME.
RUN useradd --system --create-home --shell /usr/sbin/nologin grid
USER grid
EXPOSE 4444
# ui/gunicorn_conf.py has gunicorn write its own lines in the Grid UI's log format (GRID_UI_LOG_FORMAT).
CMD ["gunicorn", "--config", "python:ui.gunicorn_conf", "--bind", "0.0.0.0:4444", "--workers", "2", "ui.app:app"]
