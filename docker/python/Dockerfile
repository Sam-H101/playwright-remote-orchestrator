# The image checks run in (main.py and the check files). Build context is the repo root.
# Playwright refuses a client and a run-server on different minor versions, so its Playwright follows
# runner/playwright (Dependabot updates it): the newest release on that major.minor, up to the pinned
# version, since the Python package skips some of npm's patch releases. The browsers are the ones that
# version installs. Ubuntu 24.04 and its Python 3.12, like Playwright's own Python image.
FROM ubuntu:noble
ARG DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# As in Playwright's image: pip from get-pip, in /usr/local where it can be upgraded, and allowed to
# install into the system Python.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 curl git openssh-client gpg \
        ca-certificates libnss3-tools p11-kit libnss3 libatomic1 \
    && rm /usr/lib/python3.12/EXTERNALLY-MANAGED \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3 1 \
    && curl -fsSL https://bootstrap.pypa.io/get-pip.py | python - --no-cache-dir \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash pwuser

# Playwright and its browsers, in a layer only a new pin rebuilds. The browser folders are open to every
# user, as in Playwright's image. From 1.65 the node bundled with the Python package needs libatomic1 (above),
# which `install --with-deps` can't add before that node runs. constraints.txt keeps the requirements below from moving Playwright.
COPY runner/playwright/package.json /tmp/playwright/package.json
RUN v=$(python -c "import json; print(json.load(open('/tmp/playwright/package.json'))['dependencies']['playwright-core'])") \
    && { python -m pip install --no-cache-dir --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org \
            "playwright>=${v%.*}.0,<=${v}" \
        || { echo "No Playwright ${v%.*} up to ${v} on PyPI? It comes out there one to five weeks after npm: build again once it has."; exit 1; }; } \
    && python -m playwright install --with-deps \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R 777 /ms-playwright \
    && python -m pip freeze | grep -i '^playwright==' > /tmp/playwright/constraints.txt \
    && cat /tmp/playwright/constraints.txt
RUN find / -type f -name "libnssckbi.so" 2>/dev/null | while read line; do\
           mv $line ${line}.bak;\
           ln -s /usr/lib/x86_64-linux-gnu/pkcs11/p11-kit-trust.so $line;\
           done

COPY docker/python/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org \
        -c /tmp/playwright/constraints.txt -r /tmp/requirements.txt
#CMD ["playwright", "run-server", "--port", "3000"]
USER root
WORKDIR /app
