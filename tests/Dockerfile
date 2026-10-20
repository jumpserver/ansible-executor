# Small integration image; the production image extends the repository's EE.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc make libc6-dev openssh-server openssh-client python3 python3-pip libpam-modules \
        ansible-core \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m pip install --break-system-packages --no-cache-dir ansible-runner==2.4.0
COPY . /src
WORKDIR /src
RUN make check && make install LIBDIR=/usr/lib \
    && /usr/local/libexec/jms-executor-start --configure-nss \
    && mkdir -p /etc/jms-executor/skel \
    && printf '* hard core 0\n* hard nproc 256\n* hard nofile 4096\n' > /etc/security/limits.d/jms-executor.conf
ENTRYPOINT ["/usr/local/libexec/jms-executor-start"]
