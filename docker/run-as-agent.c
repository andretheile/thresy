/*
 * run-as-agent: start a program as the coach's agent user.
 *
 * The bridge runs as the user "coach" and starts the coding-agent CLI as the user "agent", so
 * that the agent cannot read the bridge's environment (Telegram token, read/write database
 * URL) through /proc, cannot signal the bridge and cannot read its files. Neither user is
 * root, so this small program is installed setuid root but executable only by the coach group
 * (docker/setup-users.sh):
 *
 *     -rwsr-x---  root coach  /usr/local/libexec/thresy/run-as-agent
 *
 * Usage (only the real user id CALLER_UID may run it):
 *
 *     run-as-agent -- PROGRAM [ARG...]     execute PROGRAM as the agent user
 *     run-as-agent --signal SIGNAL PGID    send SIGNAL (a number) to the process group PGID,
 *                                          as the agent user
 *
 * What it does, in this order, and nothing else:
 *
 *  1. It refuses every caller whose real user id is not CALLER_UID.
 *  2. It drops root for good: the supplementary groups become AGENT_GID alone, then the real,
 *     effective and saved group and user ids become AGENT_GID and AGENT_UID. It checks the
 *     result and that root cannot be regained.
 *  3. It sets no_new_privs, so neither the program nor anything it starts can gain privileges
 *     again: setuid and file-capability programs (su, this helper) lose their effect.
 *  4. --signal: kill(-PGID, SIGNAL). As the agent user this reaches the agent's own processes
 *     only. PGID 0 and 1 are refused (they would mean "my group" and "every process").
 *  5. Otherwise it adds the NAME=value lines of CREDENTIALS_FILE, read as the agent user, to
 *     the environment (so the model credential need not pass through the bridge) and executes
 *     PROGRAM, searching PATH when the name has no slash.
 *
 * The environment is inherited from the caller; it never travels on a command line. Because
 * the program is setuid, the C library removes LD_PRELOAD, LD_LIBRARY_PATH, TMPDIR, TZDIR and
 * similar variables before main() runs; the executed program itself is not setuid and runs
 * normally.
 *
 * Exit status: the program's own once it runs; 126 when the caller is not permitted or a
 * privilege change failed; 127 when PROGRAM cannot be executed; 2 for a usage error. With
 * --signal: 0 sent, 3 no such process group, 1 any other failure.
 *
 * Build (docker/build-run-as-agent.sh passes the ids):
 *     cc -DCALLER_UID=10001 -DAGENT_UID=10002 -DAGENT_GID=10002 -o run-as-agent run-as-agent.c
 */

#define _GNU_SOURCE
#include <errno.h>
#include <grp.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef CALLER_UID
#error "define CALLER_UID (the bridge user's uid)"
#endif
#ifndef AGENT_UID
#error "define AGENT_UID (the agent user's uid)"
#endif
#ifndef AGENT_GID
#error "define AGENT_GID (the agent user's primary gid)"
#endif
#ifndef CREDENTIALS_FILE
#define CREDENTIALS_FILE "/home/agent/.thresy/credentials.env"
#endif

#define PROG "run-as-agent"

static void fail(int status, const char *what) {
    fprintf(stderr, PROG ": %s: %s\n", what, strerror(errno));
    exit(status);
}

static void usage(void) {
    fputs("usage: " PROG " -- PROGRAM [ARG...]\n"
          "       " PROG " --signal SIGNAL PGID\n",
          stderr);
    exit(2);
}

static void drop_privileges(void) {
    const gid_t gid = (gid_t)AGENT_GID;
    const uid_t uid = (uid_t)AGENT_UID;
    uid_t ruid, euid, suid;
    gid_t rgid, egid, sgid;

    if (setgroups(1, &gid) != 0)
        fail(126, "setgroups");
    if (setresgid(gid, gid, gid) != 0)
        fail(126, "setresgid");
    if (setresuid(uid, uid, uid) != 0)
        fail(126, "setresuid");
    if (getresuid(&ruid, &euid, &suid) != 0 || ruid != uid || euid != uid || suid != uid ||
        getresgid(&rgid, &egid, &sgid) != 0 || rgid != gid || egid != gid || sgid != gid) {
        fputs(PROG ": user or group ids are not the agent's after switching\n", stderr);
        exit(126);
    }
    if (setuid(0) == 0 || seteuid(0) == 0 || setgid(0) == 0) {
        fputs(PROG ": root could be regained after switching; refusing\n", stderr);
        exit(126);
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0)
        fail(126, "prctl(PR_SET_NO_NEW_PRIVS)");
}

static int parse_number(const char *text, long min, long max, long *out) {
    char *end;
    errno = 0;
    long value = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || value < min || value > max)
        return 0;
    *out = value;
    return 1;
}

static int valid_name(const char *name, const char *end) {
    if (name == end)
        return 0;
    for (const char *p = name; p < end; p++) {
        char c = *p;
        int letter = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '_';
        int digit = c >= '0' && c <= '9';
        if (!letter && !(digit && p != name))
            return 0;
    }
    return 1;
}

/* NAME=value lines; blank lines and lines starting with # are skipped, "export " is allowed
 * before the name, and one pair of matching quotes around the value is removed. Values are
 * never printed. */
static void load_credentials(void) {
    FILE *file = fopen(CREDENTIALS_FILE, "re");
    if (file == NULL) {
        if (errno != ENOENT)
            fprintf(stderr, PROG ": cannot read %s: %s\n", CREDENTIALS_FILE, strerror(errno));
        return;
    }
    struct stat st;
    if (fstat(fileno(file), &st) == 0 && (st.st_mode & 077) != 0)
        fprintf(stderr, PROG ": warning: %s can be read by other users (chmod 600 it)\n",
                CREDENTIALS_FILE);

    char *line = NULL;
    size_t capacity = 0;
    ssize_t length;
    unsigned long number = 0;
    while ((length = getline(&line, &capacity, file)) != -1) {
        number++;
        while (length > 0 && (line[length - 1] == '\n' || line[length - 1] == '\r'))
            line[--length] = '\0';
        char *p = line;
        while (*p == ' ' || *p == '\t')
            p++;
        if (*p == '\0' || *p == '#')
            continue;
        if (strncmp(p, "export ", 7) == 0) {
            p += 7;
            while (*p == ' ' || *p == '\t')
                p++;
        }
        char *equals = strchr(p, '=');
        if (equals == NULL || !valid_name(p, equals)) {
            fprintf(stderr, PROG ": %s line %lu ignored (expected NAME=value)\n",
                    CREDENTIALS_FILE, number);
            continue;
        }
        *equals = '\0';
        char *value = equals + 1;
        size_t size = strlen(value);
        if (size >= 2 && (value[0] == '"' || value[0] == '\'') && value[size - 1] == value[0]) {
            value[size - 1] = '\0';
            value++;
        }
        if (setenv(p, value, 1) != 0)
            fprintf(stderr, PROG ": %s line %lu: setenv: %s\n", CREDENTIALS_FILE, number,
                    strerror(errno));
    }
    if (ferror(file))
        fprintf(stderr, PROG ": error reading %s\n", CREDENTIALS_FILE);
    free(line);
    fclose(file);
}

int main(int argc, char **argv) {
    if (getuid() != (uid_t)CALLER_UID) {
        fprintf(stderr, PROG ": not permitted for user id %ld\n", (long)getuid());
        return 126;
    }
    if (argc == 4 && strcmp(argv[1], "--signal") == 0) {
        long sig, pgid;
        if (!parse_number(argv[2], 0, 64, &sig) || !parse_number(argv[3], 2, INT_MAX, &pgid))
            usage();
        drop_privileges();
        if (kill((pid_t)-pgid, (int)sig) == 0)
            return 0;
        if (errno == ESRCH)
            return 3;
        fprintf(stderr, PROG ": kill(-%ld, %ld): %s\n", pgid, sig, strerror(errno));
        return 1;
    }
    if (argc < 3 || strcmp(argv[1], "--") != 0)
        usage();
    drop_privileges();
    load_credentials();
    execvp(argv[2], &argv[2]);
    fprintf(stderr, PROG ": cannot execute %s: %s\n", argv[2], strerror(errno));
    return 127;
}
