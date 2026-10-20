FROM debian:trixie-slim AS nss-build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc make libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY . /src
WORKDIR /src
RUN make check && make install LIBDIR=/usr/lib DESTDIR=/out

FROM debian:trixie-slim AS runtime
ARG ANSIBLE_RUNNER_VERSION=2.4.0
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        openssh-server openssh-client sshpass libpam-modules \
        python3 python3-pip ansible-core ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/ssh/ssh_host_* \
    && python3 -m pip install --break-system-packages --no-cache-dir \
        "ansible-runner==${ANSIBLE_RUNNER_VERSION}"
COPY --from=nss-build /out/ /
RUN /usr/local/libexec/jms-executor-start --configure-nss \
    && mkdir -p /etc/jms-executor/skel \
    && printf '* hard core 0\n* hard nproc 256\n* hard nofile 4096\n' > /etc/security/limits.d/jms-executor.conf
EXPOSE 2222
ENTRYPOINT ["/usr/local/libexec/jms-executor-start"]
CMD []

# Test the production runtime, adding only the tools needed for NSS sanitizers.
FROM runtime AS test
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc make libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY . /src
WORKDIR /src
RUN make check

# The default build excludes the compiler and test sources.
FROM runtime AS final
