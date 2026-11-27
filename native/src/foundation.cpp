#include "switchboard/native.hpp"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <limits>
#include <regex>
#include <set>
#include <utility>

#include <dirent.h>
#include <fcntl.h>
#include <openssl/evp.h>
#include <openssl/sha.h>
#include <sqlite3.h>
#include <sys/stat.h>
#include <unistd.h>

namespace switchboard
{
namespace
{
#if defined(SQLITE_OPEN_NOFOLLOW)
constexpr int SQLITE_NO_FOLLOW = SQLITE_OPEN_NOFOLLOW;
#else
constexpr int SQLITE_NO_FOLLOW = 0;
#endif

thread_local std::optional<ExecutionLimits> active_limits;

class File
{

public:
    explicit File(const int descriptor) : descriptor_(descriptor)
    {
        if (descriptor_ < 0)
        {
            throw Error("filesystem", "cannot open the configured private file or directory");
        }
    }

    ~File()
    {
        if (close(descriptor_) != 0)
        {
            std::fputs("ERROR: closing a native file descriptor failed\n", stderr);
        }
    }

    File(const File &) = delete;
    File &operator=(const File &) = delete;
    [[nodiscard]] int get() const noexcept
    {
        return descriptor_;
    }

private:
    int descriptor_;
};

// SQLITE_OPEN_NOFOLLOW refuses a symbolic link in any component of a filename, including system links such as /var and
// /tmp on macOS. Resolving the directory once leaves the flag, like O_NOFOLLOW, to refuse only a linked file.
std::filesystem::path resolve_directory(const std::filesystem::path &file)
{
    std::error_code error;
    const auto absolute = std::filesystem::absolute(file, error);
    if (error || !absolute.has_filename())
    {
        throw Error("filesystem", "cannot resolve the directory of a native state copy");
    }
    const auto directory = std::filesystem::canonical(absolute.parent_path(), error);
    if (error)
    {
        throw Error("filesystem", "cannot resolve the directory of a native state copy");
    }
    return directory / absolute.filename();
}

struct Statement
{
    sqlite3_stmt *value = nullptr;

    ~Statement()
    {
        if (value != nullptr)
        {
            sqlite3_finalize(value);
        }
    }
};

void database_ok(const int result)
{
    if (result != SQLITE_OK)
    {
        throw Error("database", "native database operation failed; inspect permissions and available space");
    }
}

void prepare(sqlite3 *database, const std::string &sql, const std::vector<Json> &parameters, Statement &statement)
{
    if (sql.empty() || sql.size() > MAX_REQUEST_BYTES || sql.find('\0') != std::string::npos)
    {
        throw Error("database", "invalid native database statement");
    }
    const char *tail = nullptr;
    database_ok(sqlite3_prepare_v2(database, sql.c_str(), static_cast<int>(sql.size()), &statement.value, &tail));
    if (statement.value == nullptr)
    {
        throw Error("database", "native database statement is empty");
    }
    while (tail != nullptr && *tail != '\0')
    {
        if (std::isspace(static_cast<unsigned char>(*tail)) == 0)
        {
            throw Error("database", "only one native database statement is allowed");
        }
        ++tail;
    }
    if (sqlite3_bind_parameter_count(statement.value) != static_cast<int>(parameters.size()))
    {
        throw Error("database", "native database parameter count mismatch");
    }
    for (std::size_t index = 0; index < parameters.size(); ++index)
    {
        const auto &parameter = parameters[index];
        const int position = static_cast<int>(index + 1);
        int result = SQLITE_OK;
        if (parameter.is_null())
        {
            result = sqlite3_bind_null(statement.value, position);
        }
        else if (parameter.is_boolean())
        {
            result = sqlite3_bind_int(statement.value, position, parameter.get<bool>() ? 1 : 0);
        }
        else if (parameter.is_number_unsigned())
        {
            const auto number = parameter.get<std::uint64_t>();
            if (number > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
            {
                throw Error("database", "native integer parameter exceeds storage range");
            }
            result = sqlite3_bind_int64(statement.value, position, static_cast<std::int64_t>(number));
        }
        else if (parameter.is_number_integer())
        {
            result = sqlite3_bind_int64(statement.value, position, parameter.get<std::int64_t>());
        }
        else if (parameter.is_number_float() && std::isfinite(parameter.get<double>()))
        {
            result = sqlite3_bind_double(statement.value, position, parameter.get<double>());
        }
        else if (parameter.is_string())
        {
            const auto &text = parameter.get_ref<const std::string &>();
            if (text.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
            {
                throw Error("database", "native text parameter is too large");
            }
            result = sqlite3_bind_text(statement.value, position, text.data(), static_cast<int>(text.size()), SQLITE_TRANSIENT);
        }
        else
        {
            throw Error("database", "unsupported native database parameter type");
        }
        database_ok(result);
    }
}

std::size_t text_length(const std::string &text)
{
    std::size_t count = 0;
    for (std::size_t index = 0; index < text.size();)
    {
        const auto first = static_cast<unsigned char>(text[index]);
        std::size_t width = 0;
        std::uint32_t value = 0;
        if (first < 0x80)
        {
            width = 1;
            value = first;
        }
        else if ((first & 0xE0) == 0xC0)
        {
            width = 2;
            value = first & 0x1F;
        }
        else if ((first & 0xF0) == 0xE0)
        {
            width = 3;
            value = first & 0x0F;
        }
        else if ((first & 0xF8) == 0xF0)
        {
            width = 4;
            value = first & 0x07;
        }
        else
        {
            throw Error("invalid", "native text must use valid UTF-8");
        }
        if (index + width > text.size())
        {
            throw Error("invalid", "native text must use complete UTF-8 characters");
        }
        for (std::size_t offset = 1; offset < width; ++offset)
        {
            const auto next = static_cast<unsigned char>(text[index + offset]);
            if ((next & 0xC0) != 0x80)
            {
                throw Error("invalid", "native text must use valid UTF-8");
            }
            value = (value << 6) | (next & 0x3F);
        }
        if ((width == 2 && value < 0x80) || (width == 3 && value < 0x800) || (width == 4 && value < 0x10000) || value > 0x10FFFF ||
            (value >= 0xD800 && value <= 0xDFFF))
        {
            throw Error("invalid", "native text contains an invalid Unicode scalar");
        }
        index += width;
        ++count;
    }
    return count;
}

void finite_json(const Json &value, const std::size_t depth = 0)
{
    if (depth > 64 || value.is_binary() || value.is_discarded())
    {
        throw Error("invalid", "native JSON exceeds the supported depth or value types");
    }
    if (value.is_number_float() && !std::isfinite(value.get<double>()))
    {
        throw Error("invalid", "native JSON numbers must be finite");
    }
    if (value.is_string())
    {
        text_length(value.get_ref<const std::string &>());
    }
    if (value.is_structured())
    {
        for (const auto &item : value)
        {
            finite_json(item, depth + 1);
        }
        if (value.is_object())
        {
            for (auto item = value.begin(); item != value.end(); ++item)
            {
                text_length(item.key());
            }
        }
    }
}

bool matches_type(const Json &value, const std::string &type)
{
    if (type == "object")
    {
        return value.is_object();
    }
    if (type == "array")
    {
        return value.is_array();
    }
    if (type == "string")
    {
        return value.is_string();
    }
    if (type == "integer")
    {
        return value.is_number_integer();
    }
    if (type == "number")
    {
        return value.is_number();
    }
    if (type == "boolean")
    {
        return value.is_boolean();
    }
    return type == "null" && value.is_null();
}

bool equal_json(const Json &left, const Json &right)
{
    if (left.is_boolean() || right.is_boolean())
    {
        return left.type() == right.type() && left == right;
    }
    if (left.is_object() && right.is_object())
    {
        if (left.size() != right.size())
        {
            return false;
        }
        for (auto item = left.begin(); item != left.end(); ++item)
        {
            if (!right.contains(item.key()) || !equal_json(item.value(), right.at(item.key())))
            {
                return false;
            }
        }
        return true;
    }
    if (left.is_array() && right.is_array())
    {
        if (left.size() != right.size())
        {
            return false;
        }
        for (std::size_t index = 0; index < left.size(); ++index)
        {
            if (!equal_json(left[index], right[index]))
            {
                return false;
            }
        }
        return true;
    }
    return left == right;
}

void schema_definition(const Json &schema, const std::size_t depth)
{
    if (!schema.is_object() || depth > 32)
    {
        throw Error("schema", "native schemas must be bounded objects");
    }
    const std::set<std::string> supported = {"type",          "enum",     "const",     "properties",    "required",      "additionalProperties",
                                             "propertyNames", "items",    "minLength", "maxLength",     "pattern",       "minimum",
                                             "maximum",       "minItems", "maxItems",  "minProperties", "maxProperties", "description",
                                             "title",         "default",  "examples",  "format",        "deprecated",    "readOnly",
                                             "writeOnly",     "$comment", "$schema"};
    for (auto item = schema.begin(); item != schema.end(); ++item)
    {
        if (!supported.contains(item.key()) && item.key().rfind("x-", 0) != 0)
        {
            throw Error("schema", "unsupported native schema keyword: " + item.key());
        }
    }
    if (schema.contains("type"))
    {
        const auto types = schema["type"].is_string() ? Json::array({schema["type"]}) : schema["type"];
        const std::set<std::string> names = {"object", "array", "string", "integer", "number", "boolean", "null"};
        if (!types.is_array() || types.empty())
        {
            throw Error("schema", "native schema type must be a supported name or nonempty array");
        }
        for (const auto &type : types)
        {
            if (!type.is_string() || !names.contains(type.get<std::string>()))
            {
                throw Error("schema", "unsupported native schema type");
            }
        }
    }
    for (const auto *key : {"minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"})
    {
        if (schema.contains(key) &&
            (!schema[key].is_number_integer() || schema[key].get<double>() < 0 || schema[key].get<double>() > static_cast<double>(MAX_JSON_INTEGER)))
        {
            throw Error("schema", "native schema size bounds must be nonnegative integers");
        }
    }
    for (const auto *key : {"minimum", "maximum"})
    {
        if (schema.contains(key) && (!schema[key].is_number() || !std::isfinite(schema[key].get<double>())))
        {
            throw Error("schema", "native schema numeric bounds must be finite");
        }
    }
    if (schema.contains("enum") && (!schema["enum"].is_array() || schema["enum"].empty()))
    {
        throw Error("schema", "native schema enum must be a nonempty array");
    }
    if (schema.contains("additionalProperties") && !schema["additionalProperties"].is_boolean())
    {
        throw Error("schema", "native schema additionalProperties must be boolean");
    }
    if (schema.contains("required"))
    {
        if (!schema["required"].is_array())
        {
            throw Error("schema", "native schema required must be an array");
        }
        for (const auto &name : schema["required"])
        {
            if (!name.is_string())
            {
                throw Error("schema", "native schema required entries must be names");
            }
        }
    }
    if (schema.contains("properties"))
    {
        if (!schema["properties"].is_object())
        {
            throw Error("schema", "native schema properties must be an object");
        }
        for (const auto &definition : schema["properties"])
        {
            schema_definition(definition, depth + 1);
        }
    }
    for (const auto *key : {"items", "propertyNames"})
    {
        if (schema.contains(key))
        {
            schema_definition(schema[key], depth + 1);
        }
    }
    if (schema.contains("pattern"))
    {
        if (!schema["pattern"].is_string() || schema["pattern"].get_ref<const std::string &>().size() > 1024)
        {
            throw Error("schema", "native schema patterns must be bounded strings");
        }
        try
        {
            const std::regex expression(schema["pattern"].get<std::string>());
            (void)expression;
        }
        catch (const std::regex_error &)
        {
            throw Error("schema", "invalid native schema pattern");
        }
    }
}

void validate_value(const Json &value, const Json &schema, const std::size_t depth)
{
    if (depth > 64)
    {
        throw Error("invalid", "native input exceeds its depth limit");
    }
    if (schema.contains("type"))
    {
        const auto types = schema["type"].is_string() ? Json::array({schema["type"]}) : schema["type"];
        if (std::none_of(types.begin(), types.end(),
                         [&](const Json &type)
                         {
                             return matches_type(value, type.get<std::string>());
                         }))
        {
            throw Error("invalid", "native input has the wrong type");
        }
    }
    if (schema.contains("const") && !equal_json(value, schema["const"]))
    {
        throw Error("invalid", "native input no longer matches the configured binding");
    }
    if (schema.contains("enum") && std::none_of(schema["enum"].begin(), schema["enum"].end(),
                                                [&](const Json &allowed)
                                                {
                                                    return equal_json(value, allowed);
                                                }))
    {
        throw Error("invalid", "native input is outside the configured choices");
    }
    if (value.is_object())
    {
        if ((schema.contains("minProperties") && value.size() < schema["minProperties"].get<std::size_t>()) ||
            (schema.contains("maxProperties") && value.size() > schema["maxProperties"].get<std::size_t>()))
        {
            throw Error("invalid", "native input has too many or too few properties");
        }
        for (const auto &key : schema.value("required", Json::array()))
        {
            if (!value.contains(key.get<std::string>()))
            {
                throw Error("invalid", "native input is missing a required field: " + key.get<std::string>());
            }
        }
        const auto properties = schema.value("properties", Json::object());
        for (auto item = value.begin(); item != value.end(); ++item)
        {
            if (schema.contains("propertyNames"))
            {
                validate_value(Json(item.key()), schema["propertyNames"], depth + 1);
            }
            if (properties.contains(item.key()))
            {
                validate_value(item.value(), properties[item.key()], depth + 1);
            }
            else if (!schema.value("additionalProperties", true))
            {
                throw Error("invalid", "native input has an unsupported field: " + item.key());
            }
        }
    }
    if (value.is_array())
    {
        if ((schema.contains("minItems") && value.size() < schema["minItems"].get<std::size_t>()) ||
            (schema.contains("maxItems") && value.size() > schema["maxItems"].get<std::size_t>()))
        {
            throw Error("invalid", "native input array is outside its size bounds");
        }
        if (schema.contains("items"))
        {
            for (const auto &item : value)
            {
                validate_value(item, schema["items"], depth + 1);
            }
        }
    }
    if (value.is_string())
    {
        const auto &text = value.get_ref<const std::string &>();
        const auto length = text_length(text);
        if ((schema.contains("minLength") && length < schema["minLength"].get<std::size_t>()) ||
            (schema.contains("maxLength") && length > schema["maxLength"].get<std::size_t>()))
        {
            throw Error("invalid", "native input text is outside its length bounds");
        }
        if (schema.contains("pattern") && !std::regex_search(text, std::regex(schema["pattern"].get<std::string>())))
        {
            throw Error("invalid", "native input text does not match the required pattern");
        }
    }
    if (value.is_number())
    {
        const auto number = value.get<double>();
        if ((schema.contains("minimum") && number < schema["minimum"].get<double>()) ||
            (schema.contains("maximum") && number > schema["maximum"].get<double>()))
        {
            throw Error("invalid", "native input number is outside its bounds");
        }
    }
}

void directory_info(const int descriptor)
{
    struct stat info
    {
    };
    if (fstat(descriptor, &info) != 0 || !S_ISDIR(info.st_mode) || info.st_uid != getuid() || (info.st_mode & 0777) != 0700)
    {
        throw Error("filesystem", "the native directory must be owned by you with mode 700");
    }
}

void plain_filename(const std::string &filename)
{
    if (filename.empty() || filename == "." || filename == ".." || filename.find_first_of("/\\") != std::string::npos ||
        filename.find('\0') != std::string::npos)
    {
        throw Error("invalid", "choose a bounded native filename, not a path");
    }
}

std::string hexadecimal(const unsigned char *bytes, const std::size_t length)
{
    static constexpr char digits[] = "0123456789abcdef";
    std::string result;
    result.reserve(length * 2);
    for (std::size_t index = 0; index < length; ++index)
    {
        result.push_back(digits[bytes[index] >> 4]);
        result.push_back(digits[bytes[index] & 0x0F]);
    }
    return result;
}

const char *const INCOMPLETE_STATE = "native state is incomplete; restore it rather than silently recreating missing history";

std::int64_t checked_version(Database &database)
{
    const auto version = database.query("PRAGMA user_version").front()["user_version"].get<std::int64_t>();
    if (version > STATE_SCHEMA_VERSION)
    {
        throw Error("database", "native state schema version " + std::to_string(version) + " is newer than this engine supports (" +
                                    std::to_string(STATE_SCHEMA_VERSION) + "); restore a matching backup or upgrade instead of downgrading");
    }
    if (version < 0)
    {
        throw Error("database", "native state version is unsupported; do not reuse another backend's database");
    }
    return version;
}

std::set<std::string> state_tables(Database &database)
{
    std::set<std::string> result;
    for (const auto &row : database.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                                          "('native_approvals', 'native_calls', 'native_events', 'native_audit', 'native_actions')"))
    {
        result.insert(row.at("name").get<std::string>());
    }
    return result;
}

void check_complete(const std::set<std::string> &tables, const std::int64_t version)
{
    for (const auto *name : {"native_approvals", "native_calls", "native_events", "native_audit"})
    {
        if (!tables.contains(name))
        {
            throw Error("database", INCOMPLETE_STATE);
        }
    }
    if (version >= 2 && !tables.contains("native_actions"))
    {
        throw Error("database", INCOMPLETE_STATE);
    }
}

void create_core_tables(Database &database)
{
    database.execute("CREATE TABLE native_approvals (id TEXT PRIMARY KEY, tool TEXT NOT NULL, args TEXT NOT NULL, summary TEXT NOT NULL, "
                     "status TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, result TEXT, error TEXT)");
    database.execute("CREATE TABLE native_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL)");
    database.execute("CREATE INDEX native_call_budget ON native_calls(tool, state, created_at)");
    database.execute("CREATE TABLE native_events (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, type TEXT NOT NULL, "
                     "payload TEXT NOT NULL, dedupe_key TEXT UNIQUE, created_at INTEGER NOT NULL)");
    database.execute("CREATE TABLE native_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, previous_hash TEXT NOT NULL, record TEXT NOT NULL, hash TEXT NOT NULL)");
}

// Version 2: one row per non-read execution attempt. Only the owner may record a late result, and only
// uncertain rows can be reconciled; see contracts/README.md.
void create_action_journal(Database &database)
{
    database.execute("CREATE TABLE native_actions ("
                     "id TEXT PRIMARY KEY CHECK (length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'), "
                     "tool TEXT NOT NULL, "
                     "args_digest TEXT NOT NULL CHECK (length(args_digest) = 64), "
                     "args TEXT NOT NULL, "
                     "origin TEXT NOT NULL CHECK (origin IN ('assistant', 'rule', 'human')), "
                     "approval_id TEXT, "
                     "state TEXT NOT NULL CHECK (state IN ('in_flight', 'succeeded', 'failed', 'uncertain', 'reconciled_applied', "
                     "'reconciled_not_applied')), "
                     "owner TEXT NOT NULL, "
                     "started_at INTEGER NOT NULL, "
                     "deadline_at INTEGER NOT NULL, "
                     "finished_at INTEGER, "
                     "error TEXT, "
                     "resolved_at INTEGER, "
                     "resolution_note TEXT)");
    database.execute("CREATE INDEX native_actions_open ON native_actions(tool, args_digest, state)");
    database.execute("CREATE INDEX native_actions_recent ON native_actions(started_at, id)");
    database.execute("CREATE INDEX native_actions_state ON native_actions(state, deadline_at)");
}
}  // namespace

Error::Error(std::string code, std::string message) : std::runtime_error(std::move(message)), code_(std::move(code))
{
}

const std::string &Error::code() const noexcept
{
    return code_;
}

bool ExecutionLimits::cancelled() const noexcept
{
    return cancel_generation != nullptr && cancel_generation->load(std::memory_order_acquire) != started_generation;
}

bool ExecutionLimits::expired() const noexcept
{
    return std::chrono::steady_clock::now() >= deadline;
}

std::int64_t ExecutionLimits::remaining_seconds() const noexcept
{
    const auto remaining = deadline - std::chrono::steady_clock::now();
    if (remaining <= std::chrono::steady_clock::duration::zero())
    {
        return 0;
    }
    return std::chrono::ceil<std::chrono::seconds>(remaining).count();
}

ScopedExecutionLimits::ScopedExecutionLimits(const ExecutionLimits limits) : previous_(active_limits)
{
    active_limits = limits;
}

ScopedExecutionLimits::~ScopedExecutionLimits()
{
    active_limits = previous_;
}

std::optional<ExecutionLimits> current_execution_limits() noexcept
{
    return active_limits;
}

class Database::Impl
{

public:
    sqlite3 *database = nullptr;

    ~Impl()
    {
        if (database != nullptr && sqlite3_close(database) != SQLITE_OK)
        {
            std::fputs("ERROR: closing the native database failed\n", stderr);
        }
    }
};

Database::Database(const std::filesystem::path &path) : Database(path, DatabaseMode::read_write)
{
}

Database::Database(const std::filesystem::path &path, const DatabaseMode mode) : impl_(std::make_unique<Impl>())
{
    if (mode == DatabaseMode::read_only)
    {
        const auto resolved = resolve_directory(path);
        File file(open(resolved.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC));
        struct stat info
        {
        };
        if (fstat(file.get(), &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != getuid() || (info.st_mode & 0077) != 0)
        {
            throw Error("database", "a native state copy must be an owned private regular file");
        }
        unsigned char header[100]{};
        if (pread(file.get(), header, sizeof(header), 0) != static_cast<ssize_t>(sizeof(header)) || std::memcmp(header, "SQLite format 3", 16) != 0)
        {
            throw Error("database", "the native state copy is not a SQLite database");
        }
        // Bytes 18 and 19 are 1 for rollback-journal files; write-ahead-log files would need shared-memory files to be created.
        if (header[18] != 1 || header[19] != 1)
        {
            throw Error("database", "read-only native state must use rollback-journal mode, as written by a native backup");
        }
        database_ok(sqlite3_open_v2(resolved.c_str(), &impl_->database, SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX | SQLITE_NO_FOLLOW, nullptr));
        database_ok(sqlite3_busy_timeout(impl_->database, 5000));
        const auto version = checked_version(*this);
        if (version == 0)
        {
            throw Error("database", "the native state copy is unversioned");
        }
        check_complete(state_tables(*this), version);
        return;
    }
    File file(open(path.c_str(), O_CREAT | O_RDWR | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0600));
    struct stat info
    {
    };
    if (fstat(file.get(), &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != getuid() || (info.st_mode & 0077) != 0)
    {
        throw Error("database", "native state must be an owned private regular file");
    }
    database_ok(sqlite3_open_v2(path.c_str(), &impl_->database, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nullptr));
    database_ok(sqlite3_busy_timeout(impl_->database, 5000));
    execute("PRAGMA journal_mode = WAL");
    execute("PRAGMA foreign_keys = ON");
    Transaction transaction(*this);
    const auto version = checked_version(*this);
    const auto tables = state_tables(*this);
    if (version == 0)
    {
        if (!query("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").empty())
        {
            throw Error("database", "unversioned nonempty native state is refused rather than silently migrated");
        }
        create_core_tables(*this);
        create_action_journal(*this);
    }
    else if (version == 1)
    {
        check_complete(tables, version);
        if (tables.contains("native_actions"))
        {
            throw Error("database", "native state version 1 already contains an action journal; restore it rather than guessing its history");
        }
        execute("CREATE INDEX IF NOT EXISTS native_call_budget ON native_calls(tool, state, created_at)");
        create_action_journal(*this);
    }
    else
    {
        check_complete(tables, version);
    }
    if (version != STATE_SCHEMA_VERSION)
    {
        execute("PRAGMA user_version = " + std::to_string(STATE_SCHEMA_VERSION));
    }
    transaction.commit();
}

Database::~Database() = default;

void Database::backup_to(const std::filesystem::path &destination)
{
    if (!destination.is_absolute() || !destination.has_filename() || destination.string().find('\0') != std::string::npos)
    {
        throw Error("filesystem", "the native backup destination must be an absolute file path");
    }
    const auto target = resolve_directory(destination);
    const int descriptor = open(target.c_str(), O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (descriptor < 0)
    {
        throw Error("filesystem", errno == EEXIST ? "the native backup destination already exists; choose a new or empty directory"
                                                  : "the native backup destination cannot be created");
    }
    {
        const File created(descriptor);
    }
    const auto remove_partial = [&target]() noexcept
    {
        try
        {
            for (const auto *suffix : {"", "-journal", "-wal", "-shm"})
            {
                std::error_code ignored;
                std::filesystem::remove(target.string() + suffix, ignored);
            }
        }
        catch (const std::exception &)
        {
            std::fputs("ERROR: a partial native backup copy could not be removed\n", stderr);
        }
    };
    try
    {
        sqlite3 *raw = nullptr;
        const int opened = sqlite3_open_v2(target.c_str(), &raw, SQLITE_OPEN_READWRITE | SQLITE_OPEN_FULLMUTEX | SQLITE_NO_FOLLOW, nullptr);
        const auto close_copy = [](sqlite3 *handle)
        {
            if (handle != nullptr && sqlite3_close_v2(handle) != SQLITE_OK)
            {
                std::fputs("ERROR: closing a native backup copy failed\n", stderr);
            }
        };
        std::unique_ptr<sqlite3, decltype(close_copy)> copy(raw, close_copy);
        database_ok(opened);
        database_ok(sqlite3_busy_timeout(copy.get(), 5000));
        sqlite3_backup *backup = sqlite3_backup_init(copy.get(), "main", impl_->database, "main");
        if (backup == nullptr)
        {
            throw Error("database", "the native backup could not start");
        }
        // A negative page count copies every page in one step under a single read transaction: a consistent snapshot.
        int step = SQLITE_BUSY;
        for (int attempt = 0; attempt < 100 && (step == SQLITE_OK || step == SQLITE_BUSY || step == SQLITE_LOCKED); ++attempt)
        {
            if (attempt > 0 && step != SQLITE_OK)
            {
                sqlite3_sleep(50);
            }
            step = sqlite3_backup_step(backup, -1);
        }
        const int finished = sqlite3_backup_finish(backup);
        if (step != SQLITE_DONE || finished != SQLITE_OK)
        {
            throw Error("database", "the native backup copy did not complete");
        }
        {
            Statement statement;
            prepare(copy.get(), "PRAGMA journal_mode = DELETE", {}, statement);
            const bool row = sqlite3_step(statement.value) == SQLITE_ROW;
            const auto *journal = row ? reinterpret_cast<const char *>(sqlite3_column_text(statement.value, 0)) : nullptr;
            if (journal == nullptr || std::string(journal) != "delete" || sqlite3_step(statement.value) != SQLITE_DONE)
            {
                throw Error("database", "the native backup copy could not leave write-ahead-log mode");
            }
        }
        if (sqlite3_close(copy.get()) != SQLITE_OK)
        {
            throw Error("database", "the native backup copy could not be closed cleanly");
        }
        (void)copy.release();
    }
    catch (...)
    {
        remove_partial();
        throw;
    }
}

std::int64_t Database::execute(const std::string &sql, const std::vector<Json> &parameters)
{
    Statement statement;
    prepare(impl_->database, sql, parameters, statement);
    int result = SQLITE_ROW;
    while (result == SQLITE_ROW)
    {
        result = sqlite3_step(statement.value);
    }
    if (result != SQLITE_DONE)
    {
        throw Error("database", "native database write failed");
    }
    return sqlite3_changes(impl_->database);
}

std::vector<Json> Database::query(const std::string &sql, const std::vector<Json> &parameters)
{
    Statement statement;
    prepare(impl_->database, sql, parameters, statement);
    std::vector<Json> rows;
    int result = sqlite3_step(statement.value);
    while (result == SQLITE_ROW)
    {
        if (rows.size() >= 10000)
        {
            throw Error("limit", "native database result exceeds its row limit");
        }
        Json row = Json::object();
        for (int column = 0; column < sqlite3_column_count(statement.value); ++column)
        {
            const char *name = sqlite3_column_name(statement.value, column);
            switch (sqlite3_column_type(statement.value, column))
            {
            case SQLITE_INTEGER:
                row[name] = sqlite3_column_int64(statement.value, column);
                break;
            case SQLITE_FLOAT:
                row[name] = sqlite3_column_double(statement.value, column);
                break;
            case SQLITE_TEXT:
            {
                const auto *text = reinterpret_cast<const char *>(sqlite3_column_text(statement.value, column));
                const int bytes = sqlite3_column_bytes(statement.value, column);
                if (text == nullptr && bytes != 0)
                {
                    throw Error("database", "native stored text could not be decoded");
                }
                row[name] = bytes == 0 ? std::string() : std::string(text, static_cast<std::size_t>(bytes));
                break;
            }
            case SQLITE_NULL:
                row[name] = nullptr;
                break;
            default:
                throw Error("database", "native state contains an unsupported stored value");
            }
        }
        rows.push_back(std::move(row));
        result = sqlite3_step(statement.value);
    }
    if (result != SQLITE_DONE)
    {
        throw Error("database", "native database read failed");
    }
    return rows;
}

std::int64_t Database::last_insert_id() const
{
    return sqlite3_last_insert_rowid(impl_->database);
}

void Database::begin()
{
    execute("BEGIN IMMEDIATE");
}

void Database::commit()
{
    execute("COMMIT");
}

void Database::rollback() noexcept
{
    if (sqlite3_exec(impl_->database, "ROLLBACK", nullptr, nullptr, nullptr) != SQLITE_OK)
    {
        std::fputs("ERROR: native database rollback failed\n", stderr);
    }
}

Transaction::Transaction(Database &database) : database_(database)
{
    database_.begin();
}

Transaction::~Transaction()
{
    if (active_)
    {
        database_.rollback();
    }
}

void Transaction::commit()
{
    database_.commit();
    active_ = false;
}

Json parse_json(const std::string &text, const std::size_t maximum)
{
    if (text.size() > maximum)
    {
        throw Error("limit", "native JSON exceeds its byte limit");
    }
    std::vector<std::set<std::string>> keys;
    try
    {
        auto value = Json::parse(text,
                                 [&](const int depth, const Json::parse_event_t event, Json &parsed)
                                 {
                                     if (depth > 64)
                                     {
                                         throw Error("invalid", "native JSON exceeds its nesting limit");
                                     }
                                     if (event == Json::parse_event_t::object_start)
                                     {
                                         keys.emplace_back();
                                     }
                                     else if (event == Json::parse_event_t::key && !keys.back().insert(parsed.get<std::string>()).second)
                                     {
                                         throw Error("invalid", "duplicate native JSON object key");
                                     }
                                     else if (event == Json::parse_event_t::object_end)
                                     {
                                         keys.pop_back();
                                     }
                                     return true;
                                 });
        finite_json(value);
        return value;
    }
    catch (const Json::exception &)
    {
        throw Error("invalid", "native JSON is malformed or contains an unsupported number");
    }
}

std::string hash(const Json &value)
{
    finite_json(value);
    const auto text = value.dump(-1, ' ', true);
    unsigned char digest[SHA256_DIGEST_LENGTH]{};
    if (SHA256(reinterpret_cast<const unsigned char *>(text.data()), text.size(), digest) == nullptr)
    {
        throw Error("crypto", "native configuration hashing failed");
    }
    return hexadecimal(digest, sizeof(digest));
}

void check_schema(const Json &schema)
{
    finite_json(schema);
    schema_definition(schema, 0);
}

void validate(const Json &value, const Json &schema)
{
    check_schema(schema);
    finite_json(value);
    validate_value(value, schema, 0);
}

void check_object(const Json &value, const std::vector<std::string> &allowed, const std::vector<std::string> &required)
{
    if (!value.is_object())
    {
        throw Error("invalid", "expected a native configuration object");
    }
    for (auto item = value.begin(); item != value.end(); ++item)
    {
        if (std::find(allowed.begin(), allowed.end(), item.key()) == allowed.end())
        {
            throw Error("invalid", "unsupported native configuration field: " + item.key());
        }
    }
    for (const auto &key : required)
    {
        if (!value.contains(key))
        {
            throw Error("invalid", "missing native configuration field: " + key);
        }
    }
}

std::string string_field(const Json &value, const std::string &key, const std::size_t maximum, const bool required, const std::size_t minimum)
{
    if (!value.contains(key) && !required)
    {
        return {};
    }
    if (!value.contains(key) || !value[key].is_string())
    {
        throw Error("invalid", "native field must be text: " + key);
    }
    auto text = value[key].get<std::string>();
    const auto length = text_length(text);
    if (length < minimum || length > maximum || text.find('\0') != std::string::npos)
    {
        throw Error("invalid", "native text field is outside its bounds: " + key);
    }
    return text;
}

std::int64_t integer_field(const Json &value, const std::string &key, const std::int64_t minimum, const std::int64_t maximum, const std::int64_t fallback)
{
    if (!value.contains(key))
    {
        return fallback;
    }
    if (!value[key].is_number_integer() || value[key].get<double>() < static_cast<double>(minimum) || value[key].get<double>() > static_cast<double>(maximum))
    {
        throw Error("invalid", "native integer field is outside its bounds: " + key);
    }
    return value[key].get<std::int64_t>();
}

bool boolean_field(const Json &value, const std::string &key, const bool fallback)
{
    if (!value.contains(key))
    {
        return fallback;
    }
    if (!value[key].is_boolean())
    {
        throw Error("invalid", "native field must be boolean: " + key);
    }
    return value[key].get<bool>();
}

Json object_schema(const Json &properties, const std::vector<std::string> &required)
{
    return {{"type", "object"}, {"properties", properties}, {"required", required}, {"additionalProperties", false}};
}

std::string timestamp(const std::int64_t seconds)
{
    if (seconds < 0 || seconds > 253402300799LL)
    {
        throw Error("invalid", "native timestamp is outside the supported years");
    }
    const auto raw = static_cast<std::time_t>(seconds);
    std::tm calendar{};
    if (gmtime_r(&raw, &calendar) == nullptr)
    {
        throw Error("invalid", "native timestamp cannot be rendered");
    }
    // strftime into a fixed buffer: a stream would hide an allocation failure behind a shortened string.
    char text[32]{};
    const auto size = std::strftime(text, sizeof(text), "%Y-%m-%dT%H:%M:%SZ", &calendar);
    if (size == 0)
    {
        throw Error("invalid", "native timestamp cannot be rendered");
    }
    return std::string(text, size);
}

std::int64_t parse_timestamp(const std::string &value)
{
    static const std::regex pattern(R"(^([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})([Zz]|[+-][0-9]{2}:[0-9]{2})$)");
    std::smatch match;
    if (value.size() > 32 || !std::regex_match(value, match, pattern))
    {
        throw Error("invalid", "native dates require whole seconds and an explicit timezone offset");
    }
    const int year = std::stoi(match[1]);
    const unsigned int month = static_cast<unsigned int>(std::stoi(match[2]));
    const unsigned int day = static_cast<unsigned int>(std::stoi(match[3]));
    const int hour = std::stoi(match[4]);
    const int minute = std::stoi(match[5]);
    const int second = std::stoi(match[6]);
    const std::chrono::year_month_day date{std::chrono::year{year}, std::chrono::month{month}, std::chrono::day{day}};
    if (year < 1970 || !date.ok() || hour > 23 || minute > 59 || second > 59)
    {
        throw Error("invalid", "native date contains an invalid calendar or time component");
    }
    int offset = 0;
    const std::string zone = match[7];
    if (zone.size() > 1)
    {
        const int hours = std::stoi(zone.substr(1, 2));
        const int minutes = std::stoi(zone.substr(4, 2));
        if (hours > 23 || minutes > 59)
        {
            throw Error("invalid", "native date contains an invalid timezone offset");
        }
        offset = (hours * 3600 + minutes * 60) * (zone[0] == '+' ? 1 : -1);
    }
    const auto days = std::chrono::sys_days{date}.time_since_epoch();
    const auto result = std::chrono::duration_cast<std::chrono::seconds>(days).count() + hour * 3600 + minute * 60 + second - offset;
    if (result < 0 || result > 253402300799LL)
    {
        throw Error("invalid", "native date falls outside the supported instant range");
    }
    return result;
}

std::filesystem::path expand_path(const std::string &value)
{
    if (value.empty() || value.find('\0') != std::string::npos)
    {
        throw Error("invalid", "native paths must be nonempty");
    }
    std::string expanded = value;
    if (value == "~" || value.rfind("~/", 0) == 0)
    {
        const char *home = std::getenv("HOME");
        if (home == nullptr || *home != '/')
        {
            throw Error("filesystem", "an absolute HOME is required for native home-relative paths");
        }
        expanded = std::string(home) + value.substr(1);
    }
    const auto path = std::filesystem::path(expanded).lexically_normal();
    if (!path.is_absolute())
    {
        throw Error("invalid", "native paths must be absolute or start with ~/");
    }
    return path;
}

void ensure_private_directory(const std::filesystem::path &path)
{
    if (!path.is_absolute() || path == path.root_path())
    {
        throw Error("filesystem", "use a dedicated absolute native data directory");
    }
    struct stat info
    {
    };
    if (lstat(path.c_str(), &info) != 0)
    {
        if (errno != ENOENT || !std::filesystem::is_directory(path.parent_path()) || mkdir(path.c_str(), 0700) != 0)
        {
            throw Error("filesystem", "create the native directory's parent before use");
        }
    }
    File directory(open(path.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    directory_info(directory.get());
}

std::string read_private_file(const std::filesystem::path &root, const std::string &filename, const std::size_t maximum, const bool secret)
{
    if (filename.empty() || filename == "." || filename == ".." || filename.find_first_of("/\\") != std::string::npos ||
        filename.find('\0') != std::string::npos || maximum == 0 || maximum > 20 * 1024 * 1024)
    {
        throw Error("invalid", "choose a bounded native filename, not a path");
    }
    File directory(open(root.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    directory_info(directory.get());
    File file(openat(directory.get(), filename.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC));
    struct stat info
    {
    };
    if (fstat(file.get(), &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != getuid() || (info.st_mode & (secret ? 0077 : 0022)) != 0 ||
        info.st_size < 0 || static_cast<std::uint64_t>(info.st_size) > maximum)
    {
        throw Error("filesystem", "native input must be an owned, bounded regular file not writable by others");
    }
    std::string result;
    char buffer[8192];
    while (result.size() <= maximum)
    {
        const auto wanted = std::min(sizeof(buffer), maximum + 1 - result.size());
        const auto count = read(file.get(), buffer, wanted);
        if (count < 0)
        {
            if (errno == EINTR)
            {
                continue;
            }
            throw Error("filesystem", "native private input could not be read");
        }
        if (count == 0)
        {
            break;
        }
        result.append(buffer, static_cast<std::size_t>(count));
    }
    if (result.size() > maximum)
    {
        throw Error("limit", "native private input exceeds its actual byte limit");
    }
    return result;
}

std::vector<std::string> private_files(const std::filesystem::path &root, const std::size_t maximum)
{
    if (maximum == 0 || maximum > 10000)
    {
        throw Error("limit", "native directory entry limit is invalid");
    }
    File descriptor(open(root.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    directory_info(descriptor.get());
    const int duplicate = dup(descriptor.get());
    if (duplicate < 0)
    {
        throw Error("filesystem", "native collection descriptor could not be duplicated");
    }
    DIR *raw = fdopendir(duplicate);
    if (raw == nullptr)
    {
        if (close(duplicate) != 0)
        {
            std::fputs("ERROR: closing a rejected native collection failed\n", stderr);
        }
        throw Error("filesystem", "native collection cannot be listed");
    }
    const auto close_directory = [](DIR *directory)
    {
        if (closedir(directory) != 0)
        {
            std::fputs("ERROR: closing a native collection failed\n", stderr);
        }
    };
    const std::unique_ptr<DIR, decltype(close_directory)> directory(raw, close_directory);
    std::vector<std::string> result;
    errno = 0;
    while (const auto *entry = readdir(directory.get()))
    {
        const std::string name = entry->d_name;
        if (name != "." && name != "..")
        {
            if (result.size() >= maximum)
            {
                throw Error("limit", "native collection exceeds its declared entry cap");
            }
            text_length(name);
            result.push_back(name);
        }
        errno = 0;
    }
    if (errno != 0)
    {
        throw Error("filesystem", "native collection listing failed");
    }
    std::sort(result.begin(), result.end());
    return result;
}

std::string sha256_file(const std::filesystem::path &root, const std::string &filename, const std::uint64_t maximum)
{
    plain_filename(filename);
    if (maximum == 0)
    {
        throw Error("limit", "native digest input limit is invalid");
    }
    File directory(open(root.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    directory_info(directory.get());
    File file(openat(directory.get(), filename.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC));
    struct stat info
    {
    };
    if (fstat(file.get(), &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != getuid() || (info.st_mode & 0077) != 0 || info.st_size < 0 ||
        static_cast<std::uint64_t>(info.st_size) > maximum)
    {
        throw Error("filesystem", "native digest input must be an owned, private, bounded regular file");
    }
    const std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> context(EVP_MD_CTX_new(), EVP_MD_CTX_free);
    if (!context || EVP_DigestInit_ex(context.get(), EVP_sha256(), nullptr) != 1)
    {
        throw Error("crypto", "native file digest could not start");
    }
    std::vector<char> buffer(65536);
    std::uint64_t total = 0;
    for (;;)
    {
        const auto count = read(file.get(), buffer.data(), buffer.size());
        if (count < 0)
        {
            if (errno == EINTR)
            {
                continue;
            }
            throw Error("filesystem", "native digest input could not be read");
        }
        if (count == 0)
        {
            break;
        }
        total += static_cast<std::uint64_t>(count);
        if (total > maximum)
        {
            throw Error("limit", "native digest input exceeds its byte limit");
        }
        if (EVP_DigestUpdate(context.get(), buffer.data(), static_cast<std::size_t>(count)) != 1)
        {
            throw Error("crypto", "native file digest failed");
        }
    }
    unsigned char digest[EVP_MAX_MD_SIZE]{};
    unsigned int length = 0;
    if (EVP_DigestFinal_ex(context.get(), digest, &length) != 1 || length != SHA256_DIGEST_LENGTH)
    {
        throw Error("crypto", "native file digest failed");
    }
    return hexadecimal(digest, length);
}

void write_private_file(const std::filesystem::path &root, const std::string &filename, const std::string &content)
{
    plain_filename(filename);
    if (content.size() > MAX_REQUEST_BYTES)
    {
        throw Error("limit", "native private output exceeds its byte limit");
    }
    File directory(open(root.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    directory_info(directory.get());
    const int descriptor = openat(directory.get(), filename.c_str(), O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (descriptor < 0)
    {
        throw Error("filesystem", errno == EEXIST ? "native private output already exists" : "native private output cannot be created");
    }
    File file(descriptor);
    std::size_t written = 0;
    while (written < content.size())
    {
        const auto count = write(file.get(), content.data() + written, content.size() - written);
        if (count < 0)
        {
            if (errno == EINTR)
            {
                continue;
            }
            throw Error("filesystem", "native private output could not be written");
        }
        written += static_cast<std::size_t>(count);
    }
    if (fsync(file.get()) != 0 || fsync(directory.get()) != 0)
    {
        throw Error("filesystem", "native private output could not be synchronized to storage");
    }
}
}  // namespace switchboard
