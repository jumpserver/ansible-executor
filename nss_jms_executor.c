/* Virtual identities for the dedicated JumpServer OpenSSH executor (glibc NSS).
 * No account files, network requests, allocations, or filesystem side effects.
 */
#include <errno.h>
#include <grp.h>
#include <nss.h>
#include <pwd.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define UID_BASE 100000U
#define ID_LIMIT 65535U
#define NAME_SIZE 32
#define HOME_SIZE 80
#define HOME_ROOT "/home/jms-executor/"
#define LOGIN_SHELL "/usr/local/libexec/jms-executor-session"

static unsigned int parse_name(const char *name)
{
    const char *p;
    unsigned int id = 0;

    if (name == NULL || strncmp(name, "jms_", 4) != 0)
        return 0;
    p = name + 4;
    /* One canonical spelling per UID; reject signs, whitespace and leading 0s. */
    if (*p < '1' || *p > '9')
        return 0;
    for (; *p; p++) {
        if (*p < '0' || *p > '9')
            return 0;
        id = id * 10 + (unsigned int)(*p - '0');
        if (id > ID_LIMIT)
            return 0;
    }
    return id;
}

static enum nss_status not_found(int *errnop)
{
    *errnop = ENOENT;
    return NSS_STATUS_NOTFOUND;
}

static enum nss_status small_buffer(int *errnop)
{
    *errnop = ERANGE;
    return NSS_STATUS_TRYAGAIN;
}

static char *put_string(char **buffer, const char *value)
{
    char *start = *buffer;
    size_t len = strlen(value) + 1;
    memcpy(start, value, len);
    *buffer += len;
    return start;
}

static enum nss_status fill_passwd(unsigned int id, struct passwd *result,
                                 char *buffer, size_t buflen, int *errnop)
{
    char name[NAME_SIZE], home[HOME_SIZE];
    size_t needed;

    if (id == 0 || id > ID_LIMIT)
        return not_found(errnop);
    snprintf(name, sizeof(name), "jms_%u", id);
    snprintf(home, sizeof(home), HOME_ROOT "%s", name);
    needed = strlen(name) + 1 + 2 + 1 + strlen(home) + 1 + sizeof(LOGIN_SHELL);
    if (buflen < needed)
        return small_buffer(errnop);

    result->pw_name = put_string(&buffer, name);
    result->pw_passwd = put_string(&buffer, "*");
    result->pw_uid = (uid_t)(UID_BASE + id);
    result->pw_gid = (gid_t)(UID_BASE + id);
    result->pw_gecos = put_string(&buffer, "");
    result->pw_dir = put_string(&buffer, home);
    result->pw_shell = put_string(&buffer, LOGIN_SHELL);
    *errnop = 0;
    return NSS_STATUS_SUCCESS;
}

enum nss_status _nss_jms_executor_getpwnam_r(const char *name,
    struct passwd *result, char *buffer, size_t buflen, int *errnop)
{
    return fill_passwd(parse_name(name), result, buffer, buflen, errnop);
}

enum nss_status _nss_jms_executor_getpwuid_r(uid_t uid,
    struct passwd *result, char *buffer, size_t buflen, int *errnop)
{
    if (uid <= UID_BASE || uid > UID_BASE + ID_LIMIT)
        return not_found(errnop);
    return fill_passwd((unsigned int)(uid - UID_BASE), result, buffer, buflen, errnop);
}

static enum nss_status fill_group(unsigned int id, struct group *result,
                                char *buffer, size_t buflen, int *errnop)
{
    char name[NAME_SIZE];
    uintptr_t address = (uintptr_t)buffer;
    size_t padding = (sizeof(char *) - address % sizeof(char *)) % sizeof(char *);
    char **members;

    if (id == 0 || id > ID_LIMIT)
        return not_found(errnop);
    snprintf(name, sizeof(name), "jms_%u", id);
    if (buflen < padding + sizeof(char *) + strlen(name) + 1 + 2)
        return small_buffer(errnop);
    members = (char **)(buffer + padding);
    members[0] = NULL;
    buffer += padding + sizeof(char *);
    result->gr_name = put_string(&buffer, name);
    result->gr_passwd = put_string(&buffer, "*");
    result->gr_gid = (gid_t)(UID_BASE + id);
    result->gr_mem = members;
    *errnop = 0;
    return NSS_STATUS_SUCCESS;
}

enum nss_status _nss_jms_executor_getgrnam_r(const char *name,
    struct group *result, char *buffer, size_t buflen, int *errnop)
{
    return fill_group(parse_name(name), result, buffer, buflen, errnop);
}

enum nss_status _nss_jms_executor_getgrgid_r(gid_t gid,
    struct group *result, char *buffer, size_t buflen, int *errnop)
{
    if (gid <= UID_BASE || gid > UID_BASE + ID_LIMIT)
        return not_found(errnop);
    return fill_group((unsigned int)(gid - UID_BASE), result, buffer, buflen, errnop);
}

/* No supplementary groups, and no enumeration of all possible identities. */
enum nss_status _nss_jms_executor_initgroups_dyn(const char *user, gid_t group,
    long int *start, long int *size, gid_t **groups, long int limit, int *errnop)
{
    (void)group; (void)start; (void)size; (void)groups; (void)limit;
    if (!parse_name(user))
        return not_found(errnop);
    *errnop = 0;
    return NSS_STATUS_SUCCESS;
}
