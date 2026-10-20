# The Ansible EE is built with the matching JumpServer Core version.
ARG ANSIBLE_EE_IMAGE=jumpserver/ansible-executor:latest
FROM quay.io/fedora/fedora:43 AS nss-build
RUN dnf install -y gcc make glibc-devel && dnf clean all
COPY . /src
WORKDIR /src
RUN make check && make && make install DESTDIR=/out

FROM ${ANSIBLE_EE_IMAGE}
USER root
RUN dnf install -y openssh-server openssh-clients pam util-linux \
    && dnf clean all
COPY --from=nss-build /out/ /
RUN /usr/local/libexec/jms-executor-start --configure-nss \
    && mkdir -p /etc/jms-executor/skel \
    && printf '* hard core 0\n* hard nproc 256\n* hard nofile 4096\n' > /etc/security/limits.d/jms-executor.conf
EXPOSE 2222
ENTRYPOINT ["/usr/local/libexec/jms-executor-start"]
CMD []
