/* Hold a per-UID lease in sshd's privileged PAM handle, not in the job process. */
#include <errno.h>
#include <fcntl.h>
#include <pwd.h>
#include <pthread.h>
#include <security/pam_modules.h>
#include <security/pam_ext.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/file.h>
#include <sys/wait.h>
#include <unistd.h>

struct lease { int fd; uid_t uid; };
static struct lease *active_lease;

static void close_child_lease(void)
{
    /* The unprivileged sshd/session children must never inherit the lease FD. */
    if (active_lease && active_lease->fd >= 0) {
        close(active_lease->fd);
        active_lease->fd = -1;
    }
    active_lease = NULL;
}

static int clean_uid(uid_t uid)
{
    char number[32];
    pid_t pid;
    int status;
    snprintf(number, sizeof(number), "%u", (unsigned int)uid);
    pid = fork();
    if (pid == -1)
        return -1;
    if (pid == 0) {
        char *args[] = {"/usr/bin/python3", "-I",
            "/usr/local/libexec/jms-executor-clean", number, NULL};
        char *env[] = {"PATH=/usr/bin:/bin", "LANG=C.UTF-8", NULL};
        execve(args[0], args, env);
        _exit(127);
    }
    while (waitpid(pid, &status, 0) == -1) {
        if (errno != EINTR)
            return -1;
    }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
}

static void release(pam_handle_t *pamh, void *data, int error)
{
    struct lease *lease = data;
    (void)pamh; (void)error;
    if (lease->fd >= 0) {
        if (geteuid() == 0)
            (void)clean_uid(lease->uid);
        close(lease->fd);
    }
    if (active_lease == lease)
        active_lease = NULL;
    free(lease);
}

PAM_EXTERN int pam_sm_open_session(pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    const char *user;
    struct passwd *pw;
    struct lease *lease;
    char path[128];
    int result;
    (void)flags; (void)argc; (void)argv;
    if (geteuid() != 0 || pam_get_user(pamh, &user, NULL) != PAM_SUCCESS)
        return PAM_SESSION_ERR;
    pw = getpwnam(user);
    if (!pw || pw->pw_uid < 100001 || pw->pw_uid > 165535 || pw->pw_gid != pw->pw_uid)
        return PAM_SESSION_ERR;
    lease = calloc(1, sizeof(*lease));
    if (!lease)
        return PAM_BUF_ERR;
    lease->uid = pw->pw_uid;
    snprintf(path, sizeof(path), "/run/jms-executor/leases/%u", (unsigned int)lease->uid);
    lease->fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (lease->fd < 0 || flock(lease->fd, LOCK_EX | LOCK_NB) != 0) {
        if (lease->fd >= 0)
            close(lease->fd);
        free(lease);
        pam_error(pamh, "Executor identity is busy");
        return PAM_SESSION_ERR;
    }
    /* Recover a previous crashed session before making its UID available. */
    if (clean_uid(lease->uid) != 0) {
        close(lease->fd);
        free(lease);
        return PAM_SESSION_ERR;
    }
    result = pam_set_data(pamh, "jms_executor_lease", lease, release);
    if (result != PAM_SUCCESS) {
        release(pamh, lease, result);
    } else {
        if (pthread_atfork(NULL, NULL, close_child_lease) != 0)
            return PAM_SESSION_ERR;
        active_lease = lease;
    }
    return result;
}

PAM_EXTERN int pam_sm_close_session(pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    const void *data = NULL;
    struct lease *lease;
    int result;
    (void)flags; (void)argc; (void)argv;
    if (geteuid() != 0 || pam_get_data(pamh, "jms_executor_lease", &data) != PAM_SUCCESS)
        return PAM_SESSION_ERR;
    lease = (struct lease *)data;
    if (lease->fd < 0)
        return PAM_SUCCESS;
    result = clean_uid(lease->uid);
    close(lease->fd);
    lease->fd = -1;
    return result == 0 ? PAM_SUCCESS : PAM_SESSION_ERR;
}
