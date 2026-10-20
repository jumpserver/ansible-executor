#include <assert.h>
#include <stdlib.h>
#include "../nss_jms_executor.c"

int main(void)
{
    struct passwd account;
    struct group group;
    char storage[512];
    int error = 0;
    const char *invalid[] = {
        "root", "nobody", "", "jms_", "jms_0", "jms_01", "jms_+1", "jms_-1",
        "jms_1 ", "jms_ 1", "jms_1/../../root", "jms_65536", "jms_4294967297",
        "jms_9999999999999999999999999999999999999999999999", NULL,
    };

    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
        assert(_nss_jms_executor_getpwnam_r(invalid[i], &account, storage,
                                          sizeof(storage), &error) == NSS_STATUS_NOTFOUND);
        assert(_nss_jms_executor_getgrnam_r(invalid[i], &group, storage,
                                          sizeof(storage), &error) == NSS_STATUS_NOTFOUND);
    }
    assert(_nss_jms_executor_getpwuid_r(0, &account, storage, sizeof(storage),
                                      &error) == NSS_STATUS_NOTFOUND);
    assert(_nss_jms_executor_getpwuid_r(100000, &account, storage, sizeof(storage),
                                      &error) == NSS_STATUS_NOTFOUND);
    assert(_nss_jms_executor_getpwuid_r(165536, &account, storage, sizeof(storage),
                                      &error) == NSS_STATUS_NOTFOUND);
    assert(_nss_jms_executor_getgrgid_r((gid_t)-1, &group, storage, sizeof(storage),
                                      &error) == NSS_STATUS_NOTFOUND);
    for (unsigned int id = 1; id <= ID_LIMIT; id++) {
        char name[NAME_SIZE];
        snprintf(name, sizeof(name), "jms_%u", id);
        assert(_nss_jms_executor_getpwnam_r(name, &account, storage, sizeof(storage),
                                          &error) == NSS_STATUS_SUCCESS);
        assert(account.pw_uid == UID_BASE + id && account.pw_gid == account.pw_uid);
        assert(strcmp(account.pw_name, name) == 0);
        assert(strcmp(account.pw_shell, LOGIN_SHELL) == 0);
        assert(_nss_jms_executor_getpwuid_r(account.pw_uid, &account, storage,
                                          sizeof(storage), &error) == NSS_STATUS_SUCCESS);
        assert(strcmp(account.pw_name, name) == 0);
        /* Deliberately unaligned input: gr_mem must still be correctly aligned. */
        assert(_nss_jms_executor_getgrnam_r(name, &group, storage + 1,
                                          sizeof(storage) - 1, &error) == NSS_STATUS_SUCCESS);
        assert(group.gr_gid == UID_BASE + id && group.gr_mem[0] == NULL);
        assert((uintptr_t)group.gr_mem % _Alignof(char *) == 0);
        assert(strcmp(group.gr_name, name) == 0);
        assert(_nss_jms_executor_getgrgid_r(group.gr_gid, &group, storage,
                                          sizeof(storage), &error) == NSS_STATUS_SUCCESS);
        assert(strcmp(group.gr_name, name) == 0);
    }
    for (size_t size = 0; size < 160; size++) {
        memset(storage, 0x5a, sizeof(storage));
        enum nss_status result = _nss_jms_executor_getpwnam_r(
            "jms_65535", &account, storage, size, &error);
        assert(result == NSS_STATUS_SUCCESS || (result == NSS_STATUS_TRYAGAIN && error == ERANGE));
        for (size_t i = size; i < sizeof(storage); i++)
            assert(storage[i] == 0x5a);
        memset(storage, 0x5a, sizeof(storage));
        result = _nss_jms_executor_getgrnam_r("jms_65535", &group, storage + 1, size, &error);
        assert(result == NSS_STATUS_SUCCESS || (result == NSS_STATUS_TRYAGAIN && error == ERANGE));
        assert(storage[0] == 0x5a);
        for (size_t i = size + 1; i < sizeof(storage); i++)
            assert(storage[i] == 0x5a);
    }
    puts("NSS identity, canonicalization, bounds, buffer and alignment checks passed");
    return 0;
}
