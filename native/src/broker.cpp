#if defined(__linux__) && !defined(_GNU_SOURCE)
#define _GNU_SOURCE  // struct ucred for SO_PEERCRED
#endif

#include "broker_internal.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <climits>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <exception>
#include <filesystem>
#include <initializer_list>
#include <limits>
#include <memory>
#include <new>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

namespace switchboard::broker
{
namespace
{
constexpr std::size_t READ_CHUNK_BYTES = 65536;
constexpr std::size_t MESSAGE_BYTES = 300;
constexpr int LISTEN_BACKLOG = 16;
constexpr auto ACCEPT_PAUSE = std::chrono::seconds(1);
constexpr std::int64_t MAX_LISTED_ACTIONS = 100;
constexpr std::array<const char *, 5> STATUS_FIELDS{"backend", "version", "kill_switch", "pending_approvals", "open_actions"};
#if defined(PIPE_BUF)
constexpr std::size_t OUTPUT_CHUNK_BYTES = PIPE_BUF;
#else
constexpr std::size_t OUTPUT_CHUNK_BYTES = 512;
#endif
#if defined(MSG_NOSIGNAL)
constexpr int SEND_FLAGS = MSG_NOSIGNAL;
#else
constexpr int SEND_FLAGS = 0;
#endif

bool would_block(const int error) noexcept
{
#if EAGAIN == EWOULDBLOCK
    return error == EAGAIN;
#else
    return error == EAGAIN || error == EWOULDBLOCK;
#endif
}

bool blank(const std::string &line) noexcept
{
    return std::all_of(line.begin(), line.end(),
                       [](const char character)
                       {
                           return character == ' ' || character == '\t' || character == '\r';
                       });
}

// Cuts text at a UTF-8 character boundary so error reports stay short and valid.
std::string bounded(const std::string &text)
{
    if (text.size() <= MESSAGE_BYTES)
    {
        return text;
    }
    auto end = MESSAGE_BYTES;
    while (end > 0 && (static_cast<unsigned char>(text[end]) & 0xC0U) == 0x80U)
    {
        --end;
    }
    return text.substr(0, end);
}

const Json *member(const Json &object, const char *key) noexcept
{
    if (!object.is_object())
    {
        return nullptr;
    }
    const auto found = object.find(key);
    return found == object.end() ? nullptr : &*found;
}

Json request_id(const Json &message)
{
    const auto *id = member(message, "id");
    return id != nullptr && (id->is_string() || id->is_number_integer()) ? *id : Json(nullptr);
}

Json operation_name(const Json &message)
{
    const auto *op = member(message, "op");
    return op != nullptr && op->is_string() && op->get_ref<const std::string &>().size() <= 32 ? *op : Json(nullptr);
}

bool valid_tag(const Json &tag) noexcept
{
    return tag.is_number_integer() || (tag.is_string() && tag.get_ref<const std::string &>().size() <= 128);
}

// The optional operator correlation value, echoed in every response to a request that carried a valid one.
Json request_tag(const Json &message)
{
    const auto *tag = member(message, "request_id");
    return tag != nullptr && valid_tag(*tag) ? *tag : Json(nullptr);
}

Json rpc_failure(const Json &id, const int code, const char *message)
{
    return {{"jsonrpc", "2.0"}, {"id", id}, {"error", {{"code", code}, {"message", message}}}};
}

Json operator_failure(const Json &op, const Json &tag, const std::string &code, const std::string &message)
{
    return {{"ok", false}, {"op", op}, {"request_id", tag}, {"error", {{"code", code}, {"message", message}}}};
}

// Every operation also accepts the optional request_id correlation value.
void fields(const Json &request, const std::initializer_list<const char *> allowed, const std::initializer_list<const char *> required)
{
    for (auto item = request.begin(); item != request.end(); ++item)
    {
        const auto known = item.key() == "request_id" || std::any_of(allowed.begin(), allowed.end(),
                                                                     [&item](const char *name)
                                                                     {
                                                                         return item.key() == name;
                                                                     });
        if (!known)
        {
            throw Error("invalid", "operator request contains a field this operation does not accept");
        }
    }
    for (const auto *name : required)
    {
        if (!request.contains(name))
        {
            throw Error("invalid", std::string("operator request is missing field: ") + name);
        }
    }
}

Json outcome_report(const char *status, const char *tool, const char *message)
{
    return {{"status", status}, {"tool", tool}, {"message", message}};
}

// The shared protocol requires the engine, the kill switch, pending approvals and open journal entries. The
// status is reported only when complete; a policy that denies the status tool yields an error, not a gap.
Json operator_status(Runtime &runtime)
{
    auto report = runtime.call("switchboard_status", Json::object(), Origin::human);
    const auto *status = member(report, "status");
    const auto *data = member(report, "data");
    const auto complete = status != nullptr && *status == Json("ok") && data != nullptr &&
                          std::all_of(STATUS_FIELDS.begin(), STATUS_FIELDS.end(),
                                      [data](const char *key)
                                      {
                                          return data->contains(key);
                                      });
    if (!complete)
    {
        throw Error("unavailable", "the engine status could not be read; policy must not deny switchboard_status");
    }
    return report;
}

// The shared protocol lists at most 100 journal entries and takes `id` alone. Wrong field types are malformed
// requests; well-typed values the protocol refuses are reported like the engine reports them.
Json operator_actions(Runtime &runtime, const Json &request)
{
    auto filter = request;
    filter.erase("op");
    filter.erase("request_id");
    if (filter.contains("id"))
    {
        if (!filter.at("id").is_string())
        {
            throw Error("invalid", "id must be text");
        }
        if (filter.size() != 1)
        {
            return outcome_report("invalid", "actions", "id is used alone, without state or limit");
        }
        return runtime.actions(filter);
    }
    if (filter.contains("state") && !filter.at("state").is_string())
    {
        throw Error("invalid", "state must be text");
    }
    if (filter.contains("limit"))
    {
        const auto &limit = filter.at("limit");
        if (!limit.is_number_integer())
        {
            throw Error("invalid", "limit must be a whole number");
        }
        if (limit.get<double>() < 1 || limit.get<double>() > static_cast<double>(MAX_LISTED_ACTIONS))
        {
            return outcome_report("invalid", "actions", "limit must be from 1 to 100");
        }
    }
    return runtime.actions(filter);
}

std::optional<std::string> client_line(Runtime &runtime, const std::string &line)
{
    Json message;
    try
    {
        message = parse_json(line, MAX_LINE_BYTES);
    }
    catch (const Error &)
    {
        return frame(rpc_failure(nullptr, -32700, "native request could not be parsed"), Role::client);
    }
    Json response;
    try
    {
        response = runtime.rpc(message);
    }
    catch (const std::bad_alloc &)
    {
        throw;
    }
    catch (const Error &)
    {
        response = rpc_failure(request_id(message), -32603, "native request could not be completed; inspect state before retrying external effects");
    }
    catch (const std::exception &)
    {
        std::fputs("ERROR: unexpected native broker protocol failure\n", stderr);
        response = rpc_failure(request_id(message), -32603, "native internal error");
    }
    if (response.is_null())
    {
        return std::nullopt;
    }
    return frame(response, Role::client);
}

std::string operator_line(Runtime &runtime, const std::string &line)
{
    Json request;
    try
    {
        request = parse_json(line, MAX_LINE_BYTES);
    }
    catch (const Error &)
    {
        return frame(operator_failure(nullptr, nullptr, "invalid", "operator request is not valid JSON"), Role::operator_);
    }
    const auto op = operation_name(request);
    const auto tag = request_tag(request);
    try
    {
        return frame({{"ok", true}, {"op", op}, {"request_id", tag}, {"result", operator_result(runtime, request)}}, Role::operator_);
    }
    catch (const std::bad_alloc &)
    {
        throw;
    }
    catch (const Error &error)
    {
        return frame(operator_failure(op, tag, error.code(), bounded(error.what())), Role::operator_);
    }
    catch (const std::exception &)
    {
        std::fputs("ERROR: unexpected native broker operator failure\n", stderr);
        return frame(operator_failure(op, tag, "internal", "unexpected native broker failure; inspect state before retrying external effects"),
                     Role::operator_);
    }
}

void check_socket_path(const std::filesystem::path &path)
{
    const auto &text = path.native();
    if (text.empty() || text.find('\0') != std::string::npos || !path.is_absolute() || path != path.lexically_normal() || !path.has_filename() ||
        path.filename() == "." || path.filename() == ".." || text.size() >= sizeof(sockaddr_un{}.sun_path))
    {
        throw Error("invalid", "native broker socket paths must be normalized absolute file paths that fit a local socket address");
    }
}

void check_accounts(const std::vector<std::uint32_t> &uids)
{
    if (uids.empty() || uids.size() > MAX_ACCOUNTS)
    {
        throw Error("invalid", "each native broker socket must list 1 to 64 numeric user identifiers");
    }
    if (std::find(uids.begin(), uids.end(), std::numeric_limits<std::uint32_t>::max()) != uids.end())
    {
        throw Error("invalid", "native broker user identifier 4294967295 is reserved and cannot be listed");
    }
}

class Descriptor
{

public:
    Descriptor() noexcept = default;

    explicit Descriptor(const int value) noexcept : value_(value)
    {
    }

    ~Descriptor()
    {
        reset();
    }

    Descriptor(Descriptor &&other) noexcept : value_(std::exchange(other.value_, -1))
    {
    }

    Descriptor &operator=(Descriptor &&other) noexcept
    {
        if (this != &other)
        {
            reset();
            value_ = std::exchange(other.value_, -1);
        }
        return *this;
    }

    Descriptor(const Descriptor &) = delete;
    Descriptor &operator=(const Descriptor &) = delete;

    [[nodiscard]] int get() const noexcept
    {
        return value_;
    }

    [[nodiscard]] bool valid() const noexcept
    {
        return value_ >= 0;
    }

    void reset() noexcept
    {
        if (value_ >= 0 && close(value_) != 0 && errno != EINTR)
        {
            std::fputs("ERROR: closing a native broker descriptor failed\n", stderr);
        }
        value_ = -1;
    }

private:
    int value_ = -1;
};

void close_on_exec(const int descriptor)
{
    const int flags = fcntl(descriptor, F_GETFD);
    if (flags < 0 || fcntl(descriptor, F_SETFD, flags | FD_CLOEXEC) != 0)
    {
        throw Error("broker", "a native broker descriptor could not be marked close-on-exec");
    }
}

void nonblocking(const int descriptor)
{
    const int flags = fcntl(descriptor, F_GETFL);
    if (flags < 0 || fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) != 0)
    {
        throw Error("broker", "a native broker descriptor could not be made nonblocking");
    }
}

Descriptor local_socket()
{
    Descriptor result(socket(AF_UNIX, SOCK_STREAM, 0));
    if (!result.valid())
    {
        throw Error("broker", "a local socket could not be created");
    }
    close_on_exec(result.get());
    return result;
}

sockaddr_un socket_address(const std::filesystem::path &path)
{
    sockaddr_un address{};
    const auto &text = path.native();
    if (text.empty() || text.find('\0') != std::string::npos || text.size() >= sizeof(address.sun_path))
    {
        throw Error("invalid", "a native broker socket path is empty or too long for a local socket address");
    }
    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, text.c_str(), text.size() + 1);
    return address;
}

const sockaddr *generic(const sockaddr_un &address) noexcept
{
    return reinterpret_cast<const sockaddr *>(&address);
}

// SIGPIPE would end the process when a peer disappears; failed writes are handled as errors instead.
class PipeSignalScope
{

public:
    PipeSignalScope()
    {
        struct sigaction ignore
        {
        };
        ignore.sa_handler = SIG_IGN;
        sigemptyset(&ignore.sa_mask);
        if (sigaction(SIGPIPE, &ignore, &previous_) != 0)
        {
            throw Error("broker", "the native broker could not ignore broken-pipe signals");
        }
    }

    ~PipeSignalScope()
    {
        sigaction(SIGPIPE, &previous_, nullptr);
    }

    PipeSignalScope(const PipeSignalScope &) = delete;
    PipeSignalScope &operator=(const PipeSignalScope &) = delete;

private:
    struct sigaction previous_
    {
    };
};

volatile std::sig_atomic_t stop_requested = 0;
std::atomic<int> wake_descriptor{-1};
std::atomic<Runtime *> stopping_runtime{nullptr};
std::atomic<bool> broker_active{false};
static_assert(std::atomic<int>::is_always_lock_free && std::atomic<Runtime *>::is_always_lock_free,
              "signal handlers need lock-free descriptor and runtime slots");
// Runtime::cancel only increments a std::atomic<std::uint64_t>; that is signal-safe only where it is lock-free.
static_assert(std::atomic<std::uint64_t>::is_always_lock_free, "Runtime::cancel must be usable from a signal handler");

// Async-signal-safe: a sig_atomic_t store, lock-free atomic operations and one write to the nonblocking
// self-pipe. Cancelling aborts web transfers of the call in progress, so stopping does not wait for its deadline.
void request_stop(int) noexcept
{
    const int saved = errno;
    stop_requested = 1;
    Runtime *runtime = stopping_runtime.load();
    if (runtime != nullptr)
    {
        runtime->cancel();
    }
    const int descriptor = wake_descriptor.load();
    if (descriptor >= 0)
    {
        const char byte = 1;
        const auto ignored = write(descriptor, &byte, 1);
        static_cast<void>(ignored);
    }
    errno = saved;
}

class StopSignalScope
{

public:
    StopSignalScope(Runtime &runtime, const int wake)
    {
        stop_requested = 0;
        wake_descriptor.store(wake);
        stopping_runtime.store(&runtime);
        struct sigaction action
        {
        };
        action.sa_handler = request_stop;
        sigemptyset(&action.sa_mask);
        // The handler resets itself: a second signal takes the default action and ends the process at once.
        action.sa_flags = SA_RESETHAND;
        if (sigaction(SIGINT, &action, &interrupt_) != 0)
        {
            restore();
            throw Error("broker", "the native broker could not install its interrupt handler");
        }
        interrupt_installed_ = true;
        if (sigaction(SIGTERM, &action, &terminate_) != 0)
        {
            restore();
            throw Error("broker", "the native broker could not install its termination handler");
        }
        terminate_installed_ = true;
    }

    ~StopSignalScope()
    {
        restore();
    }

    StopSignalScope(const StopSignalScope &) = delete;
    StopSignalScope &operator=(const StopSignalScope &) = delete;

private:
    void restore() noexcept
    {
        if (terminate_installed_)
        {
            sigaction(SIGTERM, &terminate_, nullptr);
            terminate_installed_ = false;
        }
        if (interrupt_installed_)
        {
            sigaction(SIGINT, &interrupt_, nullptr);
            interrupt_installed_ = false;
        }
        stopping_runtime.store(nullptr);
        wake_descriptor.store(-1);
    }

    struct sigaction interrupt_
    {
    };
    struct sigaction terminate_
    {
    };
    bool interrupt_installed_ = false;
    bool terminate_installed_ = false;
};

class ActiveScope
{

public:
    ActiveScope()
    {
        if (broker_active.exchange(true))
        {
            throw Error("broker", "a native broker is already running in this process");
        }
    }

    ~ActiveScope()
    {
        broker_active.store(false);
    }

    ActiveScope(const ActiveScope &) = delete;
    ActiveScope &operator=(const ActiveScope &) = delete;
};

class MaskScope
{

public:
    explicit MaskScope(const mode_t mask) noexcept : previous_(umask(mask))
    {
    }

    ~MaskScope()
    {
        umask(previous_);
    }

    MaskScope(const MaskScope &) = delete;
    MaskScope &operator=(const MaskScope &) = delete;

private:
    mode_t previous_;
};

// The socket directory and every parent must belong to the broker account or root. The directory itself must
// not be writable by other accounts; a parent may be only if it is sticky, as /tmp is.
void check_directories(const std::filesystem::path &directory)
{
    const auto self = geteuid();
    auto current = directory;
    for (bool immediate = true;; immediate = false)
    {
        struct stat info
        {
        };
        if (lstat(current.c_str(), &info) != 0 || !S_ISDIR(info.st_mode))
        {
            throw Error("broker", "a native broker socket directory or one of its parents is not a directory");
        }
        if (info.st_uid != self && info.st_uid != 0)
        {
            throw Error("broker", "native broker socket directories and their parents must belong to the broker account or root");
        }
        const bool shared = (info.st_mode & (S_IWGRP | S_IWOTH)) != 0;
        if (shared && (immediate || (info.st_mode & S_ISVTX) == 0))
        {
            throw Error("broker", "a native broker socket directory or one of its parents is writable by other accounts");
        }
        if (!current.has_relative_path())
        {
            return;
        }
        current = current.parent_path();
    }
}

// Resolves symbolic links in the parent once, checks it, and returns the path to bind.
std::filesystem::path resolve_socket(const std::filesystem::path &configured)
{
    std::error_code error;
    const auto directory = std::filesystem::canonical(configured.parent_path(), error);
    if (error)
    {
        throw Error("broker", "a native broker socket directory does not exist or cannot be resolved");
    }
    check_directories(directory);
    const auto path = directory / configured.filename();
    static_cast<void>(socket_address(path));
    return path;
}

bool listening(const sockaddr_un &address)
{
    auto probe = local_socket();
    nonblocking(probe.get());
    if (connect(probe.get(), generic(address), sizeof(address)) == 0)
    {
        return true;
    }
    if (errno == ECONNREFUSED || errno == ENOENT)
    {
        return false;
    }
    if (would_block(errno) || errno == EINPROGRESS)
    {
        return true;
    }
    throw Error("broker", "an existing native broker socket could not be probed; inspect and remove it manually");
}

// Refuses symbolic links, other file types and other accounts' sockets; removes only a stale own socket.
void prepare_path(const std::filesystem::path &path, const sockaddr_un &address)
{
    struct stat info
    {
    };
    if (lstat(path.c_str(), &info) != 0)
    {
        if (errno == ENOENT)
        {
            return;
        }
        throw Error("broker", "a native broker socket path cannot be inspected");
    }
    if (S_ISLNK(info.st_mode))
    {
        throw Error("broker", "a native broker socket path is a symbolic link; refusing to replace it");
    }
    if (!S_ISSOCK(info.st_mode))
    {
        throw Error("broker", "a native broker socket path exists and is not a socket; refusing to replace it");
    }
    if (info.st_uid != geteuid())
    {
        throw Error("broker", "an existing native broker socket belongs to another account; refusing to replace it");
    }
    if (listening(address))
    {
        throw Error("broker", "another process is already listening on a native broker socket");
    }
    if (unlink(path.c_str()) != 0 && errno != ENOENT)
    {
        throw Error("broker", "a stale native broker socket could not be removed");
    }
}

class Listener
{

public:
    Listener(const std::filesystem::path &path, const Role role) : role_(role), path_(path)
    {
        const auto address = socket_address(path_);
        prepare_path(path_, address);
        descriptor_ = local_socket();
        nonblocking(descriptor_.get());
        {
            // Mode 0660 from the start: the owner and the socket's group may connect; peer checks still apply.
            const MaskScope mask(0117);
            if (bind(descriptor_.get(), generic(address), sizeof(address)) != 0)
            {
                throw Error("broker", "a native broker socket could not be bound; check the directory owner and mode");
            }
        }
        struct stat info
        {
        };
        if (lstat(path_.c_str(), &info) != 0 || !S_ISSOCK(info.st_mode) || info.st_uid != geteuid())
        {
            throw Error("broker", "a newly bound native broker socket could not be verified");
        }
        device_ = info.st_dev;
        inode_ = info.st_ino;
        bound_ = true;
        if (chmod(path_.c_str(), 0660) != 0 || listen(descriptor_.get(), LISTEN_BACKLOG) != 0)
        {
            remove_socket();
            throw Error("broker", "a native broker socket could not be protected or opened for connections");
        }
    }

    ~Listener()
    {
        remove_socket();
    }

    Listener(const Listener &) = delete;
    Listener &operator=(const Listener &) = delete;

    [[nodiscard]] Role role() const noexcept
    {
        return role_;
    }

    [[nodiscard]] int get() const noexcept
    {
        return descriptor_.get();
    }

private:
    // Removes only the socket this listener created; a replacement or foreign file stays in place.
    void remove_socket() noexcept
    {
        struct stat info
        {
        };
        if (bound_ && lstat(path_.c_str(), &info) == 0 && S_ISSOCK(info.st_mode) && info.st_dev == device_ && info.st_ino == inode_ &&
            unlink(path_.c_str()) != 0)
        {
            std::fputs("ERROR: the native broker could not remove its socket file\n", stderr);
        }
        bound_ = false;
    }

    Role role_;
    std::filesystem::path path_;
    Descriptor descriptor_;
    dev_t device_ = 0;
    ino_t inode_ = 0;
    bool bound_ = false;
};

struct Connection
{
    Role role = Role::client;
    Descriptor descriptor;
    LineBuffer input;
    std::string output;
    std::size_t written = 0;
    bool ended = false;     // the peer finished sending
    bool closing = false;   // close once the pending response is written
    bool finished = false;  // remove and close now
};

std::pair<Descriptor, Descriptor> wake_pipe()
{
    std::array<int, 2> ends{-1, -1};
    if (pipe(ends.data()) != 0)
    {
        throw Error("broker", "the native broker signal pipe could not be created");
    }
    Descriptor reader(ends[0]);
    Descriptor writer(ends[1]);
    for (const auto descriptor : ends)
    {
        close_on_exec(descriptor);
        nonblocking(descriptor);
    }
    return {std::move(reader), std::move(writer)};
}

void warn_about(const BrokerOptions &options)
{
    const auto self = static_cast<std::uint32_t>(geteuid());
    if (std::find(options.client_uids.begin(), options.client_uids.end(), self) != options.client_uids.end())
    {
        std::fputs("WARNING: the native broker account is also a client account; that assistant can read the broker's configuration, "
                   "credentials and database\n",
                   stderr);
    }
    const auto shared = std::any_of(options.client_uids.begin(), options.client_uids.end(),
                                    [&options](const std::uint32_t uid)
                                    {
                                        return std::find(options.operator_uids.begin(), options.operator_uids.end(), uid) != options.operator_uids.end();
                                    });
    if (shared)
    {
        std::fputs("WARNING: an account is listed for both native broker sockets; approval separation does not apply to it\n", stderr);
    }
}

class Broker
{

public:
    Broker(Runtime &runtime, const BrokerOptions &options)
        : runtime_(runtime), options_(options), wake_(wake_pipe()), signals_(runtime, wake_.second.get()), buffer_(READ_CHUNK_BYTES)
    {
        const auto client = resolve_socket(options.client_socket);
        const auto operator_path = resolve_socket(options.operator_socket);
        if (client == operator_path)
        {
            throw Error("invalid", "native broker client and operator sockets resolve to the same path");
        }
        client_ = std::make_unique<Listener>(client, Role::client);
        operator_ = std::make_unique<Listener>(operator_path, Role::operator_);
    }

    Broker(const Broker &) = delete;
    Broker &operator=(const Broker &) = delete;

    int serve()
    {
        std::vector<pollfd> descriptors;
        while (stop_requested == 0)
        {
            const auto now = std::chrono::steady_clock::now();
            const bool accepting = now >= accept_resume_;
            descriptors.clear();
            descriptors.push_back({wake_.first.get(), POLLIN, 0});
            for (const auto *listener : {client_.get(), operator_.get()})
            {
                const bool room = count(listener->role()) < limit(listener->role());
                descriptors.push_back({accepting && room ? listener->get() : -1, POLLIN, 0});
            }
            bool ready = false;
            for (const auto &connection : connections_)
            {
                short events = 0;
                if (!connection.output.empty())
                {
                    events = POLLOUT;
                }
                else if (!connection.ended && !connection.closing)
                {
                    events = POLLIN;
                }
                ready = ready || (connection.output.empty() && !connection.closing && (connection.ended || connection.input.ready()));
                descriptors.push_back({events == 0 ? -1 : connection.descriptor.get(), events, 0});
            }
            int timeout = -1;
            if (ready)
            {
                timeout = 0;
            }
            else if (!accepting)
            {
                timeout = static_cast<int>(std::chrono::duration_cast<std::chrono::milliseconds>(accept_resume_ - now).count()) + 1;
            }
            if (poll(descriptors.data(), static_cast<nfds_t>(descriptors.size()), timeout) < 0)
            {
                if (errno == EINTR)
                {
                    continue;
                }
                std::fputs("ERROR: the native broker could not wait for socket activity\n", stderr);
                return 1;
            }
            if (descriptors[0].revents != 0)
            {
                drain_wake();
            }
            if (stop_requested != 0)
            {
                break;
            }
            for (std::size_t index = 0; index < connections_.size(); ++index)
            {
                handle(connections_[index], descriptors[3 + index].revents);
            }
            if ((descriptors[1].revents & POLLIN) != 0)
            {
                accept_from(*client_);
            }
            if ((descriptors[2].revents & POLLIN) != 0)
            {
                accept_from(*operator_);
            }
            for (auto &connection : connections_)
            {
                if (stop_requested != 0)
                {
                    break;
                }
                process(connection);
            }
            connections_.erase(std::remove_if(connections_.begin(), connections_.end(),
                                              [](const Connection &connection)
                                              {
                                                  return connection.finished;
                                              }),
                               connections_.end());
        }
        std::fputs("switchboard-native: broker stopped after a termination request\n", stderr);
        return 0;
    }

private:
    [[nodiscard]] std::size_t count(const Role role) const noexcept
    {
        return static_cast<std::size_t>(std::count_if(connections_.begin(), connections_.end(),
                                                      [role](const Connection &connection)
                                                      {
                                                          return connection.role == role;
                                                      }));
    }

    [[nodiscard]] static std::size_t limit(const Role role) noexcept
    {
        return role == Role::client ? MAX_CLIENT_CONNECTIONS : MAX_OPERATOR_CONNECTIONS;
    }

    void drain_wake() noexcept
    {
        std::array<char, 64> bytes{};
        while (read(wake_.first.get(), bytes.data(), bytes.size()) > 0)
        {
        }
    }

    void refuse(const Role role, const std::optional<std::uint32_t> uid)
    {
        ++refusals_;
        // The first refusals are all reported; later ones only at powers of two, so a flood cannot fill the log.
        if (refusals_ <= 10 || (refusals_ & (refusals_ - 1)) == 0)
        {
            const auto user = uid ? std::to_string(*uid) : std::string("unknown");
            std::fprintf(stderr, "ERROR: native broker refused a %s socket peer (user %s); refusals so far: %llu\n", role_name(role), user.c_str(),
                         static_cast<unsigned long long>(refusals_));
        }
    }

    void pause_accepting()
    {
        accept_resume_ = std::chrono::steady_clock::now() + ACCEPT_PAUSE;
        ++accept_failures_;
        if (accept_failures_ <= 10 || (accept_failures_ & (accept_failures_ - 1)) == 0)
        {
            std::fputs("ERROR: the native broker could not accept a connection; pausing new connections briefly\n", stderr);
        }
    }

    void accept_from(const Listener &listener)
    {
        while (count(listener.role()) < limit(listener.role()))
        {
            Descriptor accepted(accept(listener.get(), nullptr, nullptr));
            if (!accepted.valid())
            {
                if (errno == EINTR || errno == ECONNABORTED)
                {
                    continue;
                }
                if (!would_block(errno))
                {
                    pause_accepting();
                }
                return;
            }
            const auto uid = peer_uid(accepted.get());
            if (!authorized(listener.role(), uid, options_))
            {
                refuse(listener.role(), uid);
                continue;
            }
            try
            {
                close_on_exec(accepted.get());
                nonblocking(accepted.get());
            }
            catch (const Error &)
            {
                std::fputs("ERROR: the native broker could not configure an admitted connection; closing it\n", stderr);
                continue;
            }
            Connection connection;
            connection.role = listener.role();
            connection.descriptor = std::move(accepted);
            connections_.push_back(std::move(connection));
        }
    }

    void handle(Connection &connection, const short events)
    {
        if (events == 0 || connection.finished)
        {
            return;
        }
        if (!connection.output.empty() && (events & (POLLOUT | POLLHUP | POLLERR)) != 0)
        {
            transmit(connection);
            return;
        }
        if ((events & (POLLIN | POLLHUP)) != 0)
        {
            receive(connection);
            return;
        }
        if ((events & (POLLERR | POLLNVAL)) != 0)
        {
            connection.finished = true;
        }
    }

    void receive(Connection &connection)
    {
        if (connection.ended || connection.closing || !connection.output.empty())
        {
            return;
        }
        const auto received = recv(connection.descriptor.get(), buffer_.data(), buffer_.size(), 0);
        if (received > 0)
        {
            if (!connection.input.append(std::string_view(buffer_.data(), static_cast<std::size_t>(received))))
            {
                connection.closing = true;
                respond_with(connection, overflow_response(connection.role));
            }
            return;
        }
        if (received == 0)
        {
            connection.ended = true;
            return;
        }
        if (errno != EINTR && !would_block(errno))
        {
            connection.finished = true;
        }
    }

    void transmit(Connection &connection)
    {
        while (connection.written < connection.output.size())
        {
            const auto sent =
                send(connection.descriptor.get(), connection.output.data() + connection.written, connection.output.size() - connection.written, SEND_FLAGS);
            if (sent > 0)
            {
                connection.written += static_cast<std::size_t>(sent);
                continue;
            }
            if (sent < 0 && errno == EINTR)
            {
                continue;
            }
            if (sent < 0 && would_block(errno))
            {
                return;
            }
            connection.finished = true;
            return;
        }
        // Release large reply buffers instead of keeping their capacity for the connection's lifetime.
        connection.output = std::string();
        connection.written = 0;
        if (connection.closing)
        {
            connection.finished = true;
        }
    }

    void respond_with(Connection &connection, std::string response)
    {
        connection.output = std::move(response);
        connection.written = 0;
        transmit(connection);
    }

    // Answers at most one line per connection per loop pass, so one busy peer cannot starve the others.
    void process(Connection &connection)
    {
        if (connection.finished || connection.closing || !connection.output.empty())
        {
            return;
        }
        auto line = connection.input.next();
        if (!line && connection.ended)
        {
            line = connection.input.finish();
            if (!line)
            {
                connection.finished = true;
                return;
            }
        }
        if (!line)
        {
            return;
        }
        try
        {
            auto response = respond(runtime_, connection.role, *line);
            if (response)
            {
                respond_with(connection, std::move(*response));
            }
        }
        catch (...)
        {
            std::fputs("ERROR: the native broker could not answer a request; closing that connection\n", stderr);
            connection.finished = true;
        }
    }

    Runtime &runtime_;
    const BrokerOptions &options_;
    std::pair<Descriptor, Descriptor> wake_;
    StopSignalScope signals_;
    PipeSignalScope pipe_signal_;
    std::vector<char> buffer_;
    std::unique_ptr<Listener> client_;
    std::unique_ptr<Listener> operator_;
    std::vector<Connection> connections_;
    std::chrono::steady_clock::time_point accept_resume_{};
    std::uint64_t refusals_ = 0;
    std::uint64_t accept_failures_ = 0;
};

int relay(const std::filesystem::path &path)
{
    if (path.empty() || !path.is_absolute())
    {
        throw Error("invalid", "the native broker shim needs an absolute client socket path");
    }
    const auto address = socket_address(path);
    const PipeSignalScope pipe_signal;
    auto broker = local_socket();
    while (connect(broker.get(), generic(address), sizeof(address)) != 0 && errno != EISCONN)
    {
        if (errno != EINTR)
        {
            throw Error("broker", "the native broker client socket is not available; check its path and that the broker is running");
        }
    }
    nonblocking(broker.get());
    std::vector<char> buffer(READ_CHUNK_BYTES);
    std::string upstream;
    std::size_t upstream_sent = 0;
    std::string downstream;
    std::size_t downstream_written = 0;
    LineMeter requests(MAX_LINE_BYTES);
    LineMeter replies(MAX_RESPONSE_BYTES);
    bool input_open = true;
    bool write_shut = false;
    bool broker_open = true;
    for (;;)
    {
        if (!input_open && upstream.empty() && !write_shut && broker_open)
        {
            // Tell the broker no more requests follow; it still answers the ones already sent.
            if (shutdown(broker.get(), SHUT_WR) != 0 && errno != ENOTCONN)
            {
                throw Error("broker", "the native broker shim could not finish its request stream");
            }
            write_shut = true;
        }
        if (!broker_open && downstream.empty())
        {
            break;
        }
        std::array<pollfd, 3> descriptors{};
        descriptors[0] = {input_open && upstream.empty() ? STDIN_FILENO : -1, POLLIN, 0};
        const short socket_events = static_cast<short>((broker_open && downstream.empty() ? POLLIN : 0) | (broker_open && !upstream.empty() ? POLLOUT : 0));
        descriptors[1] = {socket_events == 0 ? -1 : broker.get(), socket_events, 0};
        descriptors[2] = {downstream.empty() ? -1 : STDOUT_FILENO, POLLOUT, 0};
        if (poll(descriptors.data(), static_cast<nfds_t>(descriptors.size()), -1) < 0)
        {
            if (errno == EINTR)
            {
                continue;
            }
            throw Error("broker", "the native broker shim could not wait for input");
        }
        if ((descriptors[0].revents & (POLLIN | POLLHUP | POLLERR)) != 0)
        {
            const auto received = read(STDIN_FILENO, buffer.data(), buffer.size());
            if (received > 0)
            {
                const std::string_view bytes(buffer.data(), static_cast<std::size_t>(received));
                if (!requests.add(bytes))
                {
                    throw Error("limit", "native protocol input exceeds its byte limit; the shim is stopping");
                }
                upstream.assign(bytes.data(), bytes.size());
                upstream_sent = 0;
            }
            else if (received == 0)
            {
                input_open = false;
            }
            else if (errno != EINTR && !would_block(errno))
            {
                throw Error("broker", "native protocol input could not be read");
            }
        }
        if (!upstream.empty() && (descriptors[1].revents & (POLLOUT | POLLHUP | POLLERR)) != 0)
        {
            const auto sent = send(broker.get(), upstream.data() + upstream_sent, upstream.size() - upstream_sent, SEND_FLAGS);
            if (sent > 0)
            {
                upstream_sent += static_cast<std::size_t>(sent);
                if (upstream_sent == upstream.size())
                {
                    upstream.clear();
                    upstream_sent = 0;
                }
            }
            else if (sent < 0 && errno != EINTR && !would_block(errno))
            {
                // The broker went away; unsent requests cannot be delivered.
                broker_open = false;
                upstream.clear();
            }
        }
        if (broker_open && downstream.empty() && (descriptors[1].revents & (POLLIN | POLLHUP | POLLERR)) != 0)
        {
            const auto received = recv(broker.get(), buffer.data(), buffer.size(), 0);
            if (received > 0)
            {
                const std::string_view bytes(buffer.data(), static_cast<std::size_t>(received));
                if (!replies.add(bytes))
                {
                    throw Error("limit", "a native broker reply exceeds its byte limit; the shim is stopping");
                }
                downstream.assign(bytes.data(), bytes.size());
                downstream_written = 0;
            }
            else if (received == 0 || (errno != EINTR && !would_block(errno)))
            {
                broker_open = false;
                upstream.clear();
            }
        }
        if (!downstream.empty() && (descriptors[2].revents & (POLLOUT | POLLHUP | POLLERR)) != 0)
        {
            const auto wanted = std::min(OUTPUT_CHUNK_BYTES, downstream.size() - downstream_written);
            const auto written = write(STDOUT_FILENO, downstream.data() + downstream_written, wanted);
            if (written > 0)
            {
                downstream_written += static_cast<std::size_t>(written);
                if (downstream_written == downstream.size())
                {
                    downstream.clear();
                    downstream_written = 0;
                }
            }
            else if (written < 0 && errno != EINTR && !would_block(errno))
            {
                throw Error("output", "native protocol output could not be written");
            }
        }
    }
    if (!write_shut)
    {
        std::fputs("ERROR: the native broker closed the connection before input ended; it may have stopped, or this account is not in client_uids\n", stderr);
        return 1;
    }
    return 0;
}
}  // namespace

const char *role_name(const Role role) noexcept
{
    return role == Role::client ? "client" : "operator";
}

LineBuffer::LineBuffer(const std::size_t maximum) noexcept : maximum_(maximum)
{
}

bool LineBuffer::append(const std::string_view bytes)
{
    if (overflow_)
    {
        return false;
    }
    data_.append(bytes.data(), bytes.size());
    // Only new bytes are scanned, so a line trickled in byte by byte still costs linear time.
    while (scanned_ < data_.size())
    {
        const auto end = data_.find('\n', scanned_);
        if (end == std::string::npos)
        {
            scanned_ = data_.size();
            break;
        }
        if (end - line_start_ > maximum_)
        {
            overflow_ = true;
            return false;
        }
        line_start_ = end + 1;
        scanned_ = end + 1;
    }
    if (data_.size() - line_start_ > maximum_)
    {
        overflow_ = true;
        return false;
    }
    return true;
}

std::optional<std::string> LineBuffer::next()
{
    if (overflow_ || line_start_ == 0)
    {
        return std::nullopt;
    }
    const auto end = data_.find('\n');
    auto line = data_.substr(0, end);
    data_.erase(0, end + 1);
    scanned_ -= end + 1;
    line_start_ -= end + 1;
    return line;
}

std::optional<std::string> LineBuffer::finish()
{
    if (overflow_ || line_start_ != 0 || data_.empty())
    {
        return std::nullopt;
    }
    auto rest = std::move(data_);
    data_.clear();
    scanned_ = 0;
    return rest;
}

bool LineBuffer::ready() const noexcept
{
    return !overflow_ && line_start_ != 0;
}

bool LineBuffer::overflowed() const noexcept
{
    return overflow_;
}

LineMeter::LineMeter(const std::size_t maximum) noexcept : maximum_(maximum)
{
}

bool LineMeter::add(const std::string_view bytes) noexcept
{
    for (const char byte : bytes)
    {
        if (exceeded_)
        {
            break;
        }
        if (byte == '\n')
        {
            current_ = 0;
        }
        else if (++current_ > maximum_)
        {
            exceeded_ = true;
        }
    }
    return !exceeded_;
}

void check_options(const BrokerOptions &options)
{
    check_socket_path(options.client_socket);
    check_socket_path(options.operator_socket);
    if (options.client_socket == options.operator_socket)
    {
        throw Error("invalid", "native broker client and operator sockets must be different paths");
    }
    check_accounts(options.client_uids);
    check_accounts(options.operator_uids);
}

bool authorized(const Role role, const std::optional<std::uint32_t> uid, const BrokerOptions &options) noexcept
{
    if (!uid || *uid == std::numeric_limits<std::uint32_t>::max())
    {
        return false;
    }
    const auto &allowed = role == Role::client ? options.client_uids : options.operator_uids;
    return std::find(allowed.begin(), allowed.end(), *uid) != allowed.end();
}

bool peer_credentials_supported() noexcept
{
#if defined(__linux__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__NetBSD__) || defined(__DragonFly__)
    return true;
#else
    return false;
#endif
}

std::optional<std::uint32_t> peer_uid(const int descriptor) noexcept
{
#if defined(__linux__)
    struct ucred credentials
    {
    };
    socklen_t size = sizeof(credentials);
    if (getsockopt(descriptor, SOL_SOCKET, SO_PEERCRED, &credentials, &size) != 0 || size != sizeof(credentials))
    {
        return std::nullopt;
    }
    return static_cast<std::uint32_t>(credentials.uid);
#elif defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__NetBSD__) || defined(__DragonFly__)
    uid_t uid = 0;
    gid_t gid = 0;
    if (getpeereid(descriptor, &uid, &gid) != 0)
    {
        return std::nullopt;
    }
    return static_cast<std::uint32_t>(uid);
#else
    static_cast<void>(descriptor);
    return std::nullopt;
#endif
}

Json operator_result(Runtime &runtime, const Json &request)
{
    if (!request.is_object())
    {
        throw Error("invalid", "operator requests must be JSON objects");
    }
    const auto op = string_field(request, "op", 32, true, 1);
    if (request.contains("request_id") && !valid_tag(request.at("request_id")))
    {
        throw Error("invalid", "operator request_id must be an integer or text of at most 128 bytes");
    }
    if (op == "status")
    {
        fields(request, {"op"}, {});
        return operator_status(runtime);
    }
    if (op == "approvals")
    {
        fields(request, {"op"}, {});
        return runtime.call("approvals_pending", Json::object(), Origin::human);
    }
    if (op == "approval")
    {
        // Review before approving: the result includes the exact stored arguments that approval executes.
        fields(request, {"op", "id"}, {"id"});
        return runtime.approval(string_field(request, "id", 64, true, 1));
    }
    if (op == "approve" || op == "deny")
    {
        fields(request, {"op", "id"}, {"id"});
        const auto id = string_field(request, "id", 64, true, 1);
        return op == "approve" ? runtime.approve(id) : runtime.deny(id);
    }
    if (op == "actions")
    {
        fields(request, {"op", "state", "limit", "id"}, {});
        return operator_actions(runtime, request);
    }
    if (op == "reconcile")
    {
        fields(request, {"op", "id", "resolution", "note"}, {"id", "resolution", "note"});
        const auto id = string_field(request, "id", 64, true, 1);
        const auto resolution = string_field(request, "resolution", 32, true, 1);
        const auto note = string_field(request, "note", 4096);
        return runtime.reconcile(id, resolution, note);
    }
    if (op == "kill_switch")
    {
        fields(request, {"op", "enable"}, {});
        std::optional<bool> enable;
        if (request.contains("enable"))
        {
            enable = boolean_field(request, "enable");
        }
        return runtime.kill_switch(enable);
    }
    throw Error("invalid", "operator request names an unsupported operation");
}

std::optional<std::string> respond(Runtime &runtime, const Role role, const std::string &line)
{
    if (blank(line))
    {
        return std::nullopt;
    }
    if (role == Role::client)
    {
        return client_line(runtime, line);
    }
    return operator_line(runtime, line);
}

std::string frame(const Json &value, const Role role, const std::size_t maximum)
{
    auto text = value.dump(-1, ' ', false, Json::error_handler_t::replace);
    if (text.size() > maximum)
    {
        const auto replacement =
            role == Role::client ? rpc_failure(request_id(value), -32603, "native response exceeds the broker framing limit")
                                 : operator_failure(operation_name(value), request_tag(value), "limit", "operator response exceeds the broker framing limit");
        text = replacement.dump(-1, ' ', false, Json::error_handler_t::replace);
    }
    text.push_back('\n');
    return text;
}

std::string overflow_response(const Role role)
{
    if (role == Role::client)
    {
        return frame(rpc_failure(nullptr, -32600, "native request exceeds the broker line limit; closing the connection"), role);
    }
    return frame(operator_failure(nullptr, nullptr, "limit", "operator request exceeds the broker line limit; closing the connection"), role);
}
}  // namespace switchboard::broker

namespace switchboard
{
int run_broker(Runtime &runtime, const BrokerOptions &options)
{
    try
    {
        broker::check_options(options);
        if (!broker::peer_credentials_supported())
        {
            std::fputs("ERROR: this operating system has no supported peer-credential check; the native broker refuses to start\n", stderr);
            return 1;
        }
        const broker::ActiveScope active;
        broker::warn_about(options);
        broker::Broker server(runtime, options);
        return server.serve();
    }
    catch (const Error &error)
    {
        std::fprintf(stderr, "ERROR: %s\n", broker::bounded(error.what()).c_str());
    }
    catch (const std::bad_alloc &)
    {
        std::fputs("ERROR: native broker allocation failed\n", stderr);
    }
    catch (...)
    {
        std::fputs("ERROR: unexpected native broker failure; inspect state before retrying external effects\n", stderr);
    }
    return 1;
}

int run_broker_shim(const std::filesystem::path &client_socket)
{
    try
    {
        return broker::relay(client_socket);
    }
    catch (const Error &error)
    {
        std::fprintf(stderr, "ERROR: %s\n", broker::bounded(error.what()).c_str());
    }
    catch (const std::bad_alloc &)
    {
        std::fputs("ERROR: native broker shim allocation failed\n", stderr);
    }
    catch (...)
    {
        std::fputs("ERROR: unexpected native broker shim failure\n", stderr);
    }
    return 1;
}
}  // namespace switchboard
