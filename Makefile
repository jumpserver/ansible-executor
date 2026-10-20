CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -Werror -fstack-protector-strong -D_FORTIFY_SOURCE=2
LIBDIR ?= /usr/lib64
DESTDIR ?=

.PHONY: all install check clean
all: libnss_jms_executor.so.2 pam_jms_executor.so

libnss_jms_executor.so.2: nss_jms_executor.c
	$(CC) $(CFLAGS) -fPIC -shared -Wl,-z,relro,-z,now,-soname,libnss_jms_executor.so.2 -o $@ $<

pam_jms_executor.so: pam_jms_executor.c
	$(CC) $(CFLAGS) -fPIC -shared -Wl,-z,relro,-z,now -o $@ $< -lpam -pthread

test_nss: tests/test_nss.c nss_jms_executor.c
	$(CC) $(CFLAGS) -o $@ tests/test_nss.c

check: test_nss
	./test_nss

install: all
	install -D -m 0755 libnss_jms_executor.so.2 $(DESTDIR)$(LIBDIR)/libnss_jms_executor.so.2
	install -D -m 0755 pam_jms_executor.so $(DESTDIR)/usr/local/lib/security/pam_jms_executor.so
	install -D -m 0700 cleanup.py $(DESTDIR)/usr/local/libexec/jms-executor-clean
	install -D -m 0755 entrypoint.py $(DESTDIR)/usr/local/libexec/jms-executor-start
	install -D -m 0755 session.py $(DESTDIR)/usr/local/libexec/jms-executor-session
	install -D -m 0644 config/sshd_config $(DESTDIR)/etc/ssh/jms-executor.conf
	install -D -m 0644 config/pam_sshd $(DESTDIR)/etc/pam.d/sshd

clean:
	rm -f libnss_jms_executor.so.2 pam_jms_executor.so test_nss
