# Keep PLAYWRIGHT_VERSION in sync with requirements.txt and with the
# `playwright run-server` version on every remote node: Playwright refuses
# client/server connections across different minor versions.
ARG PLAYWRIGHT_VERSION=1.64.1
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libnss3-tools p11-kit libnss3 \
    && rm -rf /var/lib/apt/lists/*
RUN find / -type f -name "libnssckbi.so" 2>/dev/null | while read line; do\
           mv $line ${line}.bak;\
           ln -s /usr/lib/x86_64-linux-gnu/pkcs11/p11-kit-trust.so $line;\
           done

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --break-system-packages --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org \
        -r /tmp/requirements.txt
#CMD ["playwright", "run-server", "--port", "3000"]
USER root
WORKDIR /app
