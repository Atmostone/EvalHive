/* EvalHive owned-exec supervisor. No host PIDs or harness-specific behavior. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAX_PROCESSES 8192
#define MAX_INPUT (1024 * 1024)
#define NONCE_BYTES 32
#define PREFIX "\036EVALHIVE_EXEC_V1 "

static unsigned char owner_nonce[NONCE_BYTES];
static pid_t main_pid = -1;
static int main_exit = -1;
static bool main_done = false, stopped = false, signalled = false;
static char private_dir[PATH_MAX];
static char private_names[16][160];
static unsigned private_count = 0;
static volatile sig_atomic_t interrupted = 0;

static int64_t milliseconds(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}
static void wipe(void *ptr, size_t size) {
    volatile unsigned char *bytes = ptr;
    while (size--) *bytes++ = 0;
}
static bool read_exact(int fd, void *buffer, size_t size) {
    unsigned char *bytes = buffer;
    while (size) {
        ssize_t count = read(fd, bytes, size);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return false;
        bytes += count; size -= (size_t)count;
    }
    return true;
}
static bool write_all(int fd, const void *buffer, size_t size) {
    const unsigned char *bytes = buffer;
    while (size) {
        ssize_t count = write(fd, bytes, size);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return false;
        bytes += count; size -= (size_t)count;
    }
    return true;
}
static bool nonce_matches(const unsigned char *value) {
    unsigned different = 0;
    for (unsigned i = 0; i < NONCE_BYTES; i++) different |= value[i] ^ owner_nonce[i];
    return different == 0;
}
static void nonce_hex(char result[65]) {
    const char *hex = "0123456789abcdef";
    for (unsigned i = 0; i < NONCE_BYTES; i++) {
        result[i * 2] = hex[owner_nonce[i] >> 4];
        result[i * 2 + 1] = hex[owner_nonce[i] & 15];
    }
    result[64] = 0;
}
static bool notice(const char *kind, int code, int children) {
    char nonce[65], frame[256];
    nonce_hex(nonce);
    int size = snprintf(frame, sizeof(frame), PREFIX "%s %s %d %d\n", nonce, kind, code, children);
    bool ok = size > 0 && (size_t)size < sizeof(frame) && write_all(STDOUT_FILENO, frame, (size_t)size);
    wipe(nonce, sizeof(nonce)); wipe(frame, sizeof(frame));
    return ok;
}
static void reap(void) {
    int status;
    pid_t pid;
    while ((pid = waitpid(-1, &status, WNOHANG)) > 0) {
        if (pid == main_pid) {
            main_done = true;
            main_exit = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
        }
    }
}
typedef struct {pid_t pid, parent; bool owned;} Process;
static int descendants(Process *items) {
    DIR *directory = opendir("/proc");
    if (!directory) return -1;
    struct dirent *entry;
    int count = 0;
    while ((entry = readdir(directory))) {
        char *end;
        long id = strtol(entry->d_name, &end, 10);
        if (*end || id <= 0 || id > INT_MAX) continue;
        char path[64], data[4096];
        snprintf(path, sizeof(path), "/proc/%ld/stat", id);
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0) continue;
        ssize_t size = read(fd, data, sizeof(data) - 1);
        close(fd);
        if (size <= 0) continue;
        data[size] = 0;
        char *tail = strrchr(data, ')'), state;
        long parent;
        if (!tail || sscanf(tail + 1, " %c %ld", &state, &parent) != 2) continue;
        if (count == MAX_PROCESSES) {closedir(directory); return -1;}
        items[count++] = (Process){.pid = (pid_t)id, .parent = (pid_t)parent, .owned = false};
    }
    closedir(directory);
    bool changed;
    do {
        changed = false;
        for (int i = 0; i < count; i++) {
            if (items[i].owned || items[i].pid == getpid()) continue;
            bool owned = items[i].parent == getpid();
            for (int j = 0; !owned && j < count; j++) owned = items[j].owned && items[i].parent == items[j].pid;
            if (owned) {items[i].owned = true; changed = true;}
        }
    } while (changed);
    return count;
}
static bool still_owned(pid_t pid) {
    for (unsigned depth = 0; depth < MAX_PROCESSES && pid > 1; depth++) {
        if (pid == getpid()) return true;
        char path[64], data[4096], state;
        snprintf(path, sizeof(path), "/proc/%d/stat", pid);
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0) return false;
        ssize_t size = read(fd, data, sizeof(data) - 1);
        close(fd);
        if (size <= 0) return false;
        data[size] = 0;
        char *tail = strrchr(data, ')');
        long parent;
        if (!tail || sscanf(tail + 1, " %c %ld", &state, &parent) != 2 || parent == pid) return false;
        pid = (pid_t)parent;
    }
    return false;
}
static bool signal_owned(int signal_number, int *remaining) {
    Process *items = calloc(MAX_PROCESSES, sizeof(Process));
    if (!items) return false;
    int count = descendants(items);
    *remaining = 0;
    bool ok = count >= 0;
    for (int i = 0; i < count; i++) {
        if (!items[i].owned) continue;
        (*remaining)++;
        if (!signal_number) continue;
        int descriptor = (int)syscall(SYS_pidfd_open, items[i].pid, 0);
        if (descriptor < 0) {if (errno != ESRCH) ok = false; continue;}
        /* A reparented/reused PID must still be one of our descendants. */
        char path[64], data[4096];
        snprintf(path, sizeof(path), "/proc/%d/stat", items[i].pid);
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        ssize_t size = fd >= 0 ? read(fd, data, sizeof(data) - 1) : -1;
        if (fd >= 0) close(fd);
        if (size > 0) {
            data[size] = 0;
            char *tail = strrchr(data, ')'), state;
            long parent;
            if (tail && sscanf(tail + 1, " %c %ld", &state, &parent) == 2 && parent == items[i].parent && still_owned(items[i].pid)) {
                if (syscall(SYS_pidfd_send_signal, descriptor, signal_number, NULL, 0) < 0 && errno != ESRCH) ok = false;
            }
        }
        close(descriptor);
    }
    free(items);
    return ok;
}
static bool stop_children(void) {
    int remaining = 0;
    bool ok = true;
    int64_t start = milliseconds();
    do {
        reap();
        if (!signal_owned(milliseconds() - start < 1000 ? SIGTERM : SIGKILL, &remaining)) ok = false;
        if (!remaining) break;
        struct timespec delay = {.tv_nsec = 20000000};
        nanosleep(&delay, NULL);
    } while (milliseconds() - start < 5000);
    reap();
    if (!signal_owned(0, &remaining)) return false;
    return ok && remaining == 0;
}
static void remove_private(void) {
    if (!private_dir[0]) return;
    for (unsigned i = 0; i < private_count; i++) unlink(private_names[i]);
    rmdir(private_dir); private_dir[0] = 0;
}
static bool valid_env(const char *name) {
    if (!name[0] || strlen(name) > 127) return false;
    for (unsigned i = 0; name[i]; i++) {
        char c = name[i];
        if (!(c == '_' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (i && c >= '0' && c <= '9'))) return false;
    }
    return true;
}
static bool inputs(char **names, unsigned count) {
    if (!count) return true;
    strcpy(private_dir, "/tmp/evalhive-private-XXXXXX");
    if (!mkdtemp(private_dir)) return false;
    for (unsigned i = 0; i < count; i++) {
        uint32_t wire;
        if (!read_exact(STDIN_FILENO, &wire, sizeof(wire))) return false;
        uint32_t size = ntohl(wire);
        if (size > MAX_INPUT) return false;
        unsigned char *data = malloc(size ? size : 1);
        if (!data) return false;
        bool ok = read_exact(STDIN_FILENO, data, size);
        int length = snprintf(private_names[private_count], sizeof(private_names[0]), "%s/input-%u", private_dir, i);
        if (length < 0 || (size_t)length >= sizeof(private_names[0])) {wipe(data, size); free(data); return false;}
        int fd = ok ? open(private_names[private_count], O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600) : -1;
        if (fd < 0) ok = false;
        else {
            private_count++;
            ok = write_all(fd, data, size) && fsync(fd) == 0 && setenv(names[i], private_names[private_count - 1], 1) == 0;
            close(fd);
        }
        wipe(data, size); free(data);
        if (!ok) return false;
    }
    return true;
}
static bool socket_path(const char *path) {
    return strncmp(path, "/tmp/evalhive-exec-", 19) == 0 && strlen(path) < sizeof(((struct sockaddr_un *)0)->sun_path)
        && !strchr(path + 19, '/');
}
static int control(const char *path, unsigned char operation) {
    if (!socket_path(path) || !read_exact(STDIN_FILENO, owner_nonce, NONCE_BYTES)) return 2;
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (fd < 0) return 2;
    struct timeval timeout = {.tv_sec = 8};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    strcpy(address.sun_path, path);
    unsigned char packet[NONCE_BYTES + 1];
    packet[0] = operation; memcpy(packet + 1, owner_nonce, NONCE_BYTES);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) < 0 || send(fd, packet, sizeof(packet), 0) != (ssize_t)sizeof(packet)) {close(fd); return 3;}
    char result[256];
    ssize_t count = recv(fd, result, sizeof(result), 0);
    close(fd); wipe(packet, sizeof(packet)); wipe(owner_nonce, sizeof(owner_nonce));
    if (count <= 0) return 3;
    return write_all(STDOUT_FILENO, result, (size_t)count) ? 0 : 3;
}
static void on_signal(int number) {(void)number; interrupted = 1;}
typedef struct {unsigned char data[MAX_INPUT + 512]; size_t size;} Buffer;
static bool flush_buffer(int fd, Buffer *buffer) {
    while (buffer->size) {
        ssize_t size = write(fd, buffer->data, buffer->size);
        if (size > 0) {buffer->size -= (size_t)size; memmove(buffer->data, buffer->data + size, buffer->size);}
        else if (size < 0 && errno == EINTR) continue;
        else return size < 0 && (errno == EAGAIN || errno == EWOULDBLOCK);
    }
    return true;
}
static bool receive_buffer(int *fd, Buffer *buffer, int *remaining) {
    while (*fd >= 0 && buffer->size < MAX_INPUT) {
        if (remaining && *remaining == 0) {close(*fd); *fd = -1; return true;}
        size_t room = MAX_INPUT - buffer->size;
        if (remaining && *remaining > 0 && room > (size_t)*remaining) room = (size_t)*remaining;
        ssize_t size = read(*fd, buffer->data + buffer->size, room);
        if (size > 0) {buffer->size += (size_t)size; if (remaining && *remaining > 0) *remaining -= (int)size;}
        else if (size < 0 && errno == EINTR) continue;
        else if (size < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return true;
        else {close(*fd); *fd = -1; return size == 0;}
    }
    return true;
}
static void exit_record(Buffer *buffer, int children) {
    char nonce[65], frame[256];
    nonce_hex(nonce);
    int size = snprintf(frame, sizeof(frame), PREFIX "%s EXIT %d %d\n", nonce, main_exit, children);
    if (size > 0 && buffer->size + (size_t)size <= sizeof(buffer->data)) {
        memcpy(buffer->data + buffer->size, frame, (size_t)size); buffer->size += (size_t)size;
    }
    wipe(nonce, sizeof(nonce)); wipe(frame, sizeof(frame));
}
int main(int argc, char **argv) {
    if (prctl(PR_SET_DUMPABLE, 0) < 0) return 2;
    signal(SIGPIPE, SIG_IGN);
    if (argc == 2 && strcmp(argv[1], "--capabilities") == 0) {
        int fd = (int)syscall(SYS_pidfd_open, getpid(), 0);
        if (fd < 0 || syscall(SYS_pidfd_send_signal, fd, 0, NULL, 0) < 0 || prctl(PR_SET_CHILD_SUBREAPER, 1) < 0) return 2;
        close(fd); puts("{\"version\":\"1.1\",\"subreaper\":true,\"pidfd\":true,\"private_inputs\":true,\"conditional_expire\":true}"); return 0;
    }
    if (argc == 3 && strcmp(argv[1], "--status") == 0) return control(argv[2], 'I');
    if (argc == 3 && strcmp(argv[1], "--release") == 0) return control(argv[2], 'R');
    if (argc == 3 && strcmp(argv[1], "--stop") == 0) return control(argv[2], 'S');
    if (argc == 3 && strcmp(argv[1], "--expire") == 0) return control(argv[2], 'D');
    if (argc < 5 || strcmp(argv[1], "--socket") != 0 || !socket_path(argv[2])) return 2;
    char *names[16];
    unsigned count = 0;
    int program = 3;
    while (program + 1 < argc && strcmp(argv[program], "--private-env") == 0) {
        if (count == 16 || !valid_env(argv[program + 1])) return 2;
        names[count++] = argv[program + 1]; program += 2;
    }
    if (program + 1 >= argc || strcmp(argv[program], "--") != 0) return 2;
    if (!read_exact(STDIN_FILENO, owner_nonce, NONCE_BYTES) || !inputs(names, count)) {remove_private(); return 2;}
    if (prctl(PR_SET_CHILD_SUBREAPER, 1) < 0) {remove_private(); return 2;}
    int listener = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX}; strcpy(address.sun_path, argv[2]);
    if (listener < 0 || bind(listener, (struct sockaddr *)&address, sizeof(address)) < 0 || chmod(argv[2], 0600) < 0 || listen(listener, 8) < 0) {remove_private(); return 2;}
    int input[2], output[2], error[2], barrier[2];
    if (pipe2(input, O_CLOEXEC) || pipe2(output, O_CLOEXEC) || pipe2(error, O_CLOEXEC) || pipe2(barrier, O_CLOEXEC)) {unlink(argv[2]); remove_private(); return 2;}
    main_pid = fork();
    if (main_pid < 0) {unlink(argv[2]); remove_private(); return 2;}
    if (main_pid == 0) {
        wipe(owner_nonce, sizeof(owner_nonce));
        close(listener); close(input[1]); close(output[0]); close(error[0]); close(barrier[1]);
        prctl(PR_SET_PDEATHSIG, SIGKILL);
        unsigned char start;
        if (!read_exact(barrier[0], &start, 1) || getppid() == 1) _exit(125);
        close(barrier[0]); setsid();
        dup2(input[0], STDIN_FILENO); dup2(output[1], STDOUT_FILENO); dup2(error[1], STDERR_FILENO);
        close(input[0]); close(output[1]); close(error[1]);
        execvp(argv[program + 1], argv + program + 1);
        _exit(127);
    }
    close(input[0]); close(output[1]); close(error[1]); close(barrier[0]);
    fcntl(output[0], F_SETFL, O_NONBLOCK); fcntl(error[0], F_SETFL, O_NONBLOCK); fcntl(input[1], F_SETFL, O_NONBLOCK);
    signal(SIGTERM, on_signal); signal(SIGINT, on_signal);
    if (!notice("READY", (int)main_pid, 0)) {stop_children(); remove_private(); unlink(argv[2]); return 125;}
    unsigned char start = 1; write_all(barrier[1], &start, 1); close(barrier[1]);
    fcntl(STDIN_FILENO, F_SETFL, O_NONBLOCK); fcntl(STDOUT_FILENO, F_SETFL, O_NONBLOCK); fcntl(STDERR_FILENO, F_SETFL, O_NONBLOCK);
    Buffer *incoming = calloc(1, sizeof(Buffer)), *outgoing = calloc(1, sizeof(Buffer)), *errors = calloc(1, sizeof(Buffer));
    if (!incoming || !outgoing || !errors) {stop_children(); remove_private(); unlink(argv[2]); return 125;}
    int host_input = STDIN_FILENO, child_input = input[1], child_output = output[0], child_error = error[0];
    int output_remaining = -1, error_remaining = -1;
    bool retain = true, drained = false, channel_failed = false;
    int64_t eof_at = 0, release_at = 0;
    while (retain) {
        reap();
        if ((interrupted || channel_failed || (eof_at && !main_done && milliseconds() - eof_at > 5000)) && !stopped) stopped = stop_children();
        if (main_done && !drained) {
            if (child_output >= 0 && ioctl(child_output, FIONREAD, &output_remaining) < 0) {channel_failed = true; output_remaining = 0;}
            if (child_error >= 0 && ioctl(child_error, FIONREAD, &error_remaining) < 0) {channel_failed = true; error_remaining = 0;}
            if (child_input >= 0) {close(child_input); child_input = -1;}
            incoming->size = 0;
            remove_private();
            drained = true;
        }
        if (child_output >= 0 && !receive_buffer(&child_output, outgoing, &output_remaining)) channel_failed = true;
        if (child_error >= 0 && !receive_buffer(&child_error, errors, &error_remaining)) channel_failed = true;
        if (main_done && drained && child_output < 0 && child_error < 0 && !errors->size && !signalled) {
            int children;
            if (!signal_owned(0, &children)) break;
            exit_record(outgoing, children); signalled = true;
        }
        if (!flush_buffer(STDOUT_FILENO, outgoing)) {outgoing->size = 0; channel_failed = true;}
        if (!flush_buffer(STDERR_FILENO, errors)) {errors->size = 0; channel_failed = true;}
        if (child_input >= 0 && !flush_buffer(child_input, incoming)) {close(child_input); child_input = -1; incoming->size = 0;}
        if (host_input < 0 && child_input >= 0 && !incoming->size) {close(child_input); child_input = -1;}
        if (release_at && ((signalled && !outgoing->size && !errors->size) || milliseconds() - release_at > 2000)) break;
        struct pollfd items[7] = {{listener, POLLIN, 0}, {host_input >= 0 && child_input >= 0 && incoming->size < MAX_INPUT ? host_input : -1, POLLIN, 0},
            {outgoing->size < MAX_INPUT ? child_output : -1, POLLIN, 0}, {errors->size < MAX_INPUT ? child_error : -1, POLLIN, 0},
            {incoming->size ? child_input : -1, POLLOUT, 0}, {outgoing->size ? STDOUT_FILENO : -1, POLLOUT, 0}, {errors->size ? STDERR_FILENO : -1, POLLOUT, 0}};
        int polled = poll(items, 7, 50);
        if (polled < 0 && errno != EINTR) break;
        if (items[1].revents & (POLLIN | POLLHUP | POLLERR)) {
            if (!receive_buffer(&host_input, incoming, NULL)) channel_failed = true;
            if (host_input < 0 && !eof_at) eof_at = milliseconds();
        }
        if (items[0].revents & POLLIN) {
            int fd = accept4(listener, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
            if (fd >= 0) {
                struct pollfd ready = {fd, POLLIN, 0}; poll(&ready, 1, 100);
                unsigned char packet[NONCE_BYTES + 1];
                ssize_t size = recv(fd, packet, sizeof(packet), MSG_TRUNC);
                if (size == (ssize_t)sizeof(packet) && nonce_matches(packet + 1)
                        && (packet[0] == 'I' || packet[0] == 'S' || packet[0] == 'R' || packet[0] == 'D')) {
                    bool ok = true;
                    /* Reap immediately before the conditional deadline action.
                     * A completed main process retains its background services. */
                    reap();
                    if (packet[0] == 'S' || packet[0] == 'R' || (packet[0] == 'D' && !main_done)) {ok = stop_children(); stopped = ok;}
                    reap(); int children = 0;
                    if (!signal_owned(0, &children)) ok = false;
                    char reply[256];
                    int bytes = snprintf(reply, sizeof(reply), "{\"main_done\":%s,\"exit_code\":%d,\"children\":%d,\"stopped\":%s,\"ok\":%s}\n",
                        main_done ? "true" : "false", main_exit, children, stopped ? "true" : "false", ok ? "true" : "false");
                    if (packet[0] == 'R' && ok) release_at = milliseconds();
                    send(fd, reply, (size_t)bytes, MSG_NOSIGNAL);
                }
                wipe(packet, sizeof(packet)); close(fd);
            }
        }
    }
    bool clean = stop_children();
    remove_private(); close(listener); unlink(argv[2]); wipe(owner_nonce, sizeof(owner_nonce));
    wipe(incoming, sizeof(Buffer)); wipe(outgoing, sizeof(Buffer)); wipe(errors, sizeof(Buffer));
    free(incoming); free(outgoing); free(errors);
    return clean && main_done ? main_exit : 125;
}
