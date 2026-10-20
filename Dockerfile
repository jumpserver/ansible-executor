ARG BASE_IMAGE=debian:trixie-slim
FROM ${BASE_IMAGE} AS base
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        openssh-server openssh-client sshpass libpam-modules \
        python3 python3-pip ca-certificates iputils-ping \
        libmariadb3 libpq5 libsybdb5 libkrb5-3 libldap2 libsasl2-2 libx11-6 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/ssh/ssh_host_*

FROM base AS build
RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
        build-essential libpam0g-dev python3-dev pkg-config \
        default-libmysqlclient-dev libpq-dev freetds-dev libkrb5-dev \
        libldap2-dev libsasl2-dev libx11-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-runtime.txt /tmp/requirements-runtime.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m pip wheel --wheel-dir /wheels -r /tmp/requirements-runtime.txt
COPY . /src
WORKDIR /src
RUN make check && make install LIBDIR=/usr/lib DESTDIR=/out \
    && python3 -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM base AS runtime
COPY --from=build /out/ /
# Keep Debian-owned Python packages intact; application wheels live in /usr/local.
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    python3 -m pip install --break-system-packages --ignore-installed --no-cache-dir --no-index /wheels/*.whl \
    && python3 -m pip check
RUN /usr/local/libexec/jms-executor-start --configure-nss \
    && mkdir -p /etc/jms-executor/skel \
    && printf '* hard core 0\n* hard nproc 256\n* hard nofile 4096\n' > /etc/security/limits.d/jms-executor.conf
EXPOSE 2222
ENTRYPOINT ["/usr/local/libexec/jms-executor-start"]
CMD []

# Test the production runtime, adding only the tools needed for NSS sanitizers.
FROM runtime AS test
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc make libc6-dev libpam0g-dev \
    && rm -rf /var/lib/apt/lists/*
COPY . /src
WORKDIR /src
RUN make check

# The default build excludes the compiler and test sources.
FROM runtime AS final
