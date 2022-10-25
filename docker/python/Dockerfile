FROM mcr.microsoft.com/playwright:v1.25.2
RUN apt-get install -y ca-certificates
WORKDIR /root
RUN npm install playwright@1.25.2 && ./node_modules/.bin/playwright install
RUN apt-get update
RUN apt install -y libnss3-tools p11-kit libnss3
RUN find / -type f -name "libnssckbi.so" 2>/dev/null | while read line; do\
           mv $line ${line}.bak;\
           ln -s /usr/lib/x86_64-linux-gnu/pkcs11/p11-kit-trust.so $line;\
           done

RUN apt-get install python3-pip -y
RUN pip install --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org pip persist-queue pytest PyYAML playwright pytest-playwright
#CMD ["./node_modules/.bin/playwright", "run-server", "--port","3000"]
USER root
WORKDIR /app
