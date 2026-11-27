#include "support.hpp"

#include <fstream>
#include <functional>
#include <utility>

namespace
{
using namespace switchboard;
using switchboard::test::expect;
using switchboard::test::FakeHttp;

// Tests create only scoped synthetic workspace fixtures.
constexpr std::int64_t base_time = 1700000000;
const std::string due_now = "2023-11-14T22:13:20Z";

class Workspace
{
public:
    Workspace()
    {
        static std::uint64_t sequence = 0;
        path = std::filesystem::current_path() / (".native-personal-test-" + std::to_string(getpid()) + "-" + std::to_string(++sequence));
        if (mkdir(path.c_str(), 0700) != 0)
        {
            throw std::runtime_error("cannot create private synthetic personal fixture");
        }
    }

    ~Workspace()
    {
        std::error_code error;
        std::filesystem::remove_all(path, error);
        if (error)
        {
            std::cerr << "ERROR: personal fixture cleanup failed\n";
        }
    }

    Workspace(const Workspace &) = delete;
    Workspace &operator=(const Workspace &) = delete;
    std::filesystem::path path;
};

Json configuration(const Workspace &directory, const std::vector<std::string> &enabled, const Json &document_options = Json::object(),
                   const Json &task_options = Json::object())
{
    Json documents{{"collections", {{{"name", "notes"}, {"path", (directory.path / "notes").string()}}}}};
    documents.update(document_options);
    return {{"switchboard", {{"connectors", enabled}, {"data_dir", directory.path.string()}}},
            {"connectors", {{"documents", documents}, {"tasks", task_options}}},
            {"policy", {{"tools", Json::object()}}},
            {"mcp", {{"expose", {"*"}}}}};
}

void write_fixture(const std::filesystem::path &path, const std::string &body)
{
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output.write(body.data(), static_cast<std::streamsize>(body.size()));
    output.close();
    expect(static_cast<bool>(output), "synthetic note write must succeed");
    expect(chmod(path.c_str(), 0600) == 0, "synthetic note must have private permissions");
}

template <typename Callable> void expect_error(Callable &&callable, const std::string &message, const std::string &code = "")
{
    bool failed = false;
    try
    {
        callable();
    }
    catch (const Error &error)
    {
        expect(code.empty() || error.code() == code, message + ": unexpected error code");
        failed = true;
    }
    expect(failed, message);
}

struct Fixture
{
    Workspace directory;
    Json config;
    Database database{directory.path / "personal-test.sqlite3"};
    FakeHttp http;
    std::int64_t time = base_time;
    std::function<void()> on_clock;
    Context context;
    Registry registry;

    explicit Fixture(const std::vector<std::string> &enabled = {"documents", "tasks"}, const Json &document_options = Json::object(),
                     const Json &task_options = Json::object())
        : config(configuration(directory, enabled, document_options, task_options)), context(config, directory.path, database, http,
                                                                                             [this]
                                                                                             {
                                                                                                 if (on_clock)
                                                                                                 {
                                                                                                     auto callback = std::move(on_clock);
                                                                                                     on_clock = {};
                                                                                                     callback();
                                                                                                 }
                                                                                                 return time;
                                                                                             })
    {
        register_personal(context, registry);
        expect(http.requests.empty(), "registration must not request any transport");
    }

    std::filesystem::path notes() const
    {
        return directory.path / "notes";
    }

    void note(const std::string &name, const std::string &body)
    {
        if (!std::filesystem::exists(notes()))
        {
            expect(mkdir(notes().c_str(), 0700) == 0, "synthetic collection creation must succeed");
        }
        write_fixture(notes() / name, body);
    }

    const Tool &tool(const std::string &name) const
    {
        const auto *selected = registry.find(name);
        expect(selected != nullptr, "expected personal tool must be registered");
        return *selected;
    }

    Json call(const std::string &name, const Json &arguments = Json::object())
    {
        try
        {
            const auto &selected = tool(name);
            validate(arguments, selected.schema);
            const auto result = selected.handler(arguments);
            expect(http.requests.empty(), "local tools must not attempt a transport request");
            return result;
        }
        catch (...)
        {
            expect(http.requests.empty(), "failing local tools must not attempt a transport request");
            throw;
        }
    }

    void poll()
    {
        expect(context.jobs.size() == 1 && context.jobs.front().name == "tasks.reminders", "only the local task reminder job must be registered");
        try
        {
            context.jobs.front().handler();
            expect(http.requests.empty(), "reminder queueing must not request any transport");
        }
        catch (...)
        {
            expect(http.requests.empty(), "failing reminder queueing must not request any transport");
            throw;
        }
    }

    std::int64_t event_count(const std::string &type)
    {
        return database.query("SELECT COUNT(*) AS n FROM native_events WHERE source = ? AND type = ?", {"tasks", type}).front()["n"].get<std::int64_t>();
    }
};

Json edit(Fixture &fixture, const Json &item, Json fields)
{
    fields["id"] = item["id"];
    fields["revision"] = item["revision"];
    return fixture.call("tasks_update", fields);
}

void test_independent_registration_and_no_scan()
{
    Fixture documents({"documents"});
    expect(!std::filesystem::exists(documents.notes()), "registration must not create or inspect a collection");
    expect(documents.registry.find("tasks_add") == nullptr && documents.context.jobs.empty(), "documents alone must not enable task tools or jobs");
    expect(documents.database.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'native_tasks_%'").empty(),
           "documents alone must not create task tables");
    const auto inventory = documents.call("documents_collections");
    expect(inventory["collections"].size() == 1 && inventory["availability_checked"] == false,
           "collection inventory must describe configured scope without availability claims");
    expect(inventory.dump().find(documents.notes().string()) == std::string::npos, "collection inventory must not disclose absolute root paths");
    expect_error(
        [&]
        {
            documents.call("documents_list", {{"collection", "notes"}});
        },
        "missing collection must fail only when accessed");

    Fixture tasks({"tasks"}, {{"collections", "invalid but disabled"}});
    expect(tasks.registry.find("documents_read") == nullptr, "tasks alone must not enable document tools");
    expect(tasks.context.jobs.size() == 1 && tasks.context.jobs.front().interval_seconds >= 30, "tasks must register one bounded poll interval");
    expect(tasks.tool("tasks_read").tier == Tier::read && tasks.tool("tasks_add").tier == Tier::draft && tasks.tool("tasks_update").tier == Tier::draft &&
               tasks.tool("tasks_delete").tier == Tier::act,
           "local reads, drafts, and exact deletion must retain their policy tiers");

    Fixture disabled({}, {{"recursive", true}}, {{"provider", "unsupported but disabled"}});
    expect(disabled.registry.tools().empty() && disabled.context.jobs.empty(), "disabled modules must neither parse their settings nor register work");
    expect(documents.http.requests.empty() && tasks.http.requests.empty() && disabled.http.requests.empty(),
           "registration and inventory must never contact a service");
}

void test_configuration_rejection()
{
    for (const auto &options : std::vector<Json>{{{"recursive", true}},
                                                 {{"max_entries", true}},
                                                 {{"max_entries", 0}},
                                                 {{"max_entries", 1001}},
                                                 {{"max_file_bytes", 1048577}},
                                                 {{"max_search_bytes", 8388609}},
                                                 {{"max_return_chars", 20001}},
                                                 {{"collections", Json::array()}}})
    {
        expect_error(
            [&]
            {
                Fixture fixture({"documents"}, options);
            },
            "unsupported document settings must fail");
    }
    for (const auto &path : std::vector<std::string>{"/", ".", "~/notes", "relative/notes", "//server/notes", "/a/../notes", "/a/./notes", "/a\\notes",
                                                     "/a:notes", "/a\nnotes", std::filesystem::current_path().string()})
    {
        const Json options{{"collections", {{{"name", "notes"}, {"path", path}}}}};
        expect_error(
            [&]
            {
                Fixture fixture({"documents"}, options);
            },
            "unsafe configured root must fail");
    }
    for (const auto &options : std::vector<Json>{{{"poll_interval_seconds", 29}},
                                                 {{"poll_interval_seconds", 3601}},
                                                 {{"poll_interval_seconds", true}},
                                                 {{"max_reminders_per_tick", 0}},
                                                 {{"max_reminders_per_tick", 501}},
                                                 {{"recurrence", "daily"}}})
    {
        expect_error(
            [&]
            {
                Fixture fixture({"tasks"}, Json::object(), options);
            },
            "unsupported task settings must fail");
    }
    Fixture fixture({"documents"});
    const auto root = fixture.notes().string();
    fixture.config["connectors"]["documents"]["collections"] = {{{"name", "one"}, {"path", root}}, {{"name", "two"}, {"path", root + "/"}}};
    expect_error(
        [&]
        {
            fixture.call("documents_collections");
        },
        "normalized duplicate roots must fail");
    fixture.config["connectors"]["documents"]["collections"] = {{{"name", "notes"}, {"path", root}, {"extensions", {".pdf"}}}};
    expect_error(
        [&]
        {
            fixture.call("documents_collections");
        },
        "undeclared binary formats must fail");
}

void test_document_paths_links_and_private_modes()
{
    Fixture fixture({"documents"});
    fixture.note("safe.md", "visible literal text");
    for (const auto &filename : std::vector<std::string>{"../safe.md", "/safe.md", "a/safe.md", "a\\safe.md", "https:safe.md", ".hidden.md", "safe.pdf"})
    {
        expect_error(
            [&]
            {
                fixture.call("documents_read", {{"collection", "notes"}, {"filename", filename}});
            },
            "a basename request must not select arbitrary paths or undeclared formats");
    }
    expect_error(
        [&]
        {
            fixture.call("documents_read", {{"collection", fixture.notes().string()}, {"filename", "safe.md"}});
        },
        "a configured path is not a collection identifier");
    expect_error(
        [&]
        {
            fixture.tool("documents_read").handler({{"collection", "notes"}, {"filename", "safe.md"}, {"path", fixture.notes().string()}});
        },
        "handlers must reject unsupported path fields even without registry validation");

    std::filesystem::create_symlink(fixture.notes() / "safe.md", fixture.notes() / "link.md");
    expect_error(
        [&]
        {
            fixture.call("documents_read", {{"collection", "notes"}, {"filename", "link.md"}});
        },
        "symbolic-link notes must not be read");
    std::filesystem::create_hard_link(fixture.notes() / "safe.md", fixture.directory.path / "alias.md");
    expect_error(
        [&]
        {
            fixture.call("documents_read", {{"collection", "notes"}, {"filename", "safe.md"}});
        },
        "a note with an outside hard link must not be read");
    std::filesystem::remove(fixture.directory.path / "alias.md");
    expect(mkdir((fixture.notes() / "directory.txt").c_str(), 0700) == 0, "synthetic subdirectory must exist");
    expect(mkfifo((fixture.notes() / "pipe.txt").c_str(), 0600) == 0, "synthetic nonregular source must exist");
    expect_error(
        [&]
        {
            fixture.call("documents_read", {{"collection", "notes"}, {"filename", "pipe.txt"}});
        },
        "nonregular input must fail without a blocking read");
    const auto listing = fixture.call("documents_list", {{"collection", "notes"}});
    expect(listing["notes"].size() == 1 && listing["notes"][0]["filename"] == "safe.md" && listing["skipped"]["unsafe_or_unavailable"] == 3,
           "listing must not treat links, directories, or pipes as note candidates");
    const auto search = fixture.call("documents_search", {{"collection", "notes"}, {"query", "literal"}});
    expect(search["coverage_complete"] == false && search["results"].size() == 1, "unsafe supported-name entries must prevent a complete search claim");

    expect(chmod((fixture.notes() / "safe.md").c_str(), 0644) == 0, "synthetic mode change must succeed");
    expect_error(
        [&]
        {
            fixture.call("documents_read", {{"collection", "notes"}, {"filename", "safe.md"}});
        },
        "shared-mode files must not be read");
    expect(chmod((fixture.notes() / "safe.md").c_str(), 0600) == 0, "synthetic private mode must be restored");
    std::filesystem::create_directory_symlink(fixture.notes(), fixture.directory.path / "linked-root");
    fixture.config["connectors"]["documents"]["collections"][0]["path"] = (fixture.directory.path / "linked-root").string();
    expect_error(
        [&]
        {
            fixture.call("documents_list", {{"collection", "notes"}});
        },
        "a symlink collection root must fail");

    expect(mkdir((fixture.directory.path / "real-parent").c_str(), 0700) == 0, "synthetic parent must exist");
    expect(mkdir((fixture.directory.path / "real-parent" / "notes").c_str(), 0700) == 0, "synthetic descendant must exist");
    write_fixture(fixture.directory.path / "real-parent" / "notes" / "safe.md", "do not follow this link");
    std::filesystem::create_directory_symlink(fixture.directory.path / "real-parent", fixture.directory.path / "parent-link");
    fixture.config["connectors"]["documents"]["collections"][0]["path"] = (fixture.directory.path / "parent-link" / "notes").string();
    expect_error(
        [&]
        {
            fixture.call("documents_read", {{"collection", "notes"}, {"filename", "safe.md"}});
        },
        "an observed ancestor symlink must not authorize a read");
}

void test_document_bounds_paging_and_invalid_text()
{
    Fixture fixture({"documents"}, {{"max_entries", 2}, {"max_file_bytes", 12}, {"max_search_bytes", 1024}, {"max_return_chars", 3}});
    fixture.note("a.md", "\xEF\xBB\xBF"
                         "A\xC3\xA9\xF0\x9F\x98\x80"
                         "Z");
    fixture.note("b.txt", std::string(13, 'x'));
    const auto page = fixture.call("documents_read", {{"collection", "notes"}, {"filename", "a.md"}, {"offset", 1}, {"max_chars", 2}});
    expect(page["text"] == "\xC3\xA9\xF0\x9F\x98\x80" && page["total_chars"] == 4 && page["size_bytes"] == 11 && page["next_offset"] == 3 &&
               page["has_more"] == true,
           "character paging must remove an initial mark without splitting encoded characters");
    expect(fixture.call("documents_read", {{"collection", "notes"}, {"filename", "a.md"}, {"offset", 99}})["text"] == "",
           "an offset beyond the current end must return an empty bounded page");
    expect(fixture.call("documents_list", {{"collection", "notes"}})["text_validated"] == false,
           "candidate listing must not claim to validate text or file sizes");
    expect_error(
        [&]
        {
            fixture.call("documents_read", {{"collection", "notes"}, {"filename", "b.txt"}});
        },
        "oversized files must not return valid-looking prefixes");
    const auto search = fixture.call("documents_search", {{"collection", "notes"}, {"query", "A"}});
    expect(search["coverage_complete"] == false && search["skipped"]["unavailable_or_unsafe_or_oversized"] == 1,
           "oversized supported files must visibly limit coverage");
    fixture.note(".ignored", "all entries count");
    expect_error(
        [&]
        {
            fixture.call("documents_list", {{"collection", "notes"}});
        },
        "hidden entries must count toward the inventory cap", "limit");
    expect_error(
        [&]
        {
            fixture.call("documents_search", {{"collection", "notes"}, {"query", "A"}});
        },
        "entry overflow must fail search rather than return partial snippets", "limit");
    expect(fixture.call("documents_read", {{"collection", "notes"}, {"filename", "a.md"}})["total_chars"] == 4,
           "known-basename reads must not require enumerating an overfull collection");

    Fixture invalid({"documents"});
    const std::vector<std::string> bodies{"\xC0\xAF", "\xED\xA0\x80", "\xF0\x9F", std::string("a\0b", 3), "\xC2\x85", "valid prefix\x01"};
    for (std::size_t index = 0; index < bodies.size(); ++index)
    {
        const auto name = "bad-" + std::to_string(index) + ".txt";
        invalid.note(name, bodies[index]);
        expect_error(
            [&]
            {
                invalid.call("documents_read", {{"collection", "notes"}, {"filename", name}, {"max_chars", 1}});
            },
            "invalid text must fail even when the requested prefix looks valid");
    }
    invalid.note("good.md", "a clean note");
    const auto result = invalid.call("documents_search", {{"collection", "notes"}, {"query", "a"}});
    expect(result["skipped"]["invalid_text"] == bodies.size() && result["coverage_complete"] == false && result["results"].size() == 1 &&
               result["files_searched"] == 1,
           "invalid full-file text must be counted, not searched as a valid prefix");
}

void test_document_literal_search_and_budget()
{
    Fixture fixture({"documents"}, {{"max_file_bytes", 128}, {"max_search_bytes", 1024}, {"max_return_chars", 5}});
    fixture.note("a.md", "A.*[x]\nalpha ALPHA hello!");
    fixture.note("b.MD", "alpha");
    fixture.note("filename-needle.md", "other words");
    fixture.note("not-declared.txt", "alpha");
    fixture.config["connectors"]["documents"]["collections"][0]["extensions"] = {".md"};
    const auto literal = fixture.call("documents_search", {{"collection", "notes"}, {"query", ".*[x]"}});
    expect(literal["literal"] == true && literal["case_sensitive"] == true && literal["results"].size() == 1 && literal["results"][0]["match_offset"] == 1 &&
               literal["results"][0]["snippet"] == ".*[x]",
           "punctuation must be searched literally, not interpreted as a pattern");
    expect(fixture.call("documents_search", {{"collection", "notes"}, {"query", "a.*"}})["results"].empty(),
           "literal search must not apply wildcard or regular-expression rules");
    expect(fixture.call("documents_search", {{"collection", "notes"}, {"query", "needle"}})["results"].empty(),
           "filename-only matches must not count as content matches");
    const auto matches = fixture.call("documents_search", {{"collection", "notes"}, {"query", "alpha"}});
    expect(matches["coverage_complete"] == true && matches["matches_in_scanned_files"] == 2 && matches["returned_text_chars"] == 5 &&
               matches["results"][1]["text_omitted"] == true && matches["skipped"]["unsupported_name"] == 1,
           "snippets must share the return budget while reporting declared scope");
    const auto first = fixture.call("documents_search", {{"collection", "notes"}, {"query", "alpha"}, {"limit", 1}});
    const auto second = fixture.call("documents_search", {{"collection", "notes"}, {"query", "alpha"}, {"limit", 1}, {"offset", first["next_offset"]}});
    expect(first["next_offset"] == 1 && second["results"][0]["filename"] == "b.MD" && second["next_offset"].is_null(),
           "search paging must page matching files rather than occurrences or unscanned bytes");
    expect(fixture.call("documents_search", {{"collection", "notes"}, {"query", "ALPHA"}})["results"].size() == 1,
           "case-sensitive search must not fold text case");

    Fixture bounded({"documents"}, {{"max_file_bytes", 16}, {"max_search_bytes", 40}});
    bounded.note("a.md", "0123456789");
    bounded.note("b.md", "0");
    const auto partial = bounded.call("documents_search", {{"collection", "notes"}, {"query", "0"}});
    expect(partial["budget_exhausted"] == true && partial["coverage_complete"] == false && partial["files_searched"] == 1 &&
               partial["files_not_searched"] == 1 && partial["read_bytes_known"] == 20 && partial["read_budget_charged_bytes"] == 20,
           "conservative reservations and verification reads must bound and disclose partial search");
    bounded.config["connectors"]["documents"]["max_search_bytes"] = 33;
    const auto stopped = bounded.call("documents_search", {{"collection", "notes"}, {"query", "0"}});
    expect(stopped["files_searched"] == 0 && stopped["budget_exhausted"] == true && stopped["read_budget_charged_bytes"] == 0,
           "an insufficient aggregate budget must not be silently raised");
    expect_error(
        [&]
        {
            bounded.call("documents_search", {{"collection", "notes"}, {"query", "x"}, {"limit", 51}});
        },
        "result ceilings must be enforced");
}

void test_document_revocation_and_live_reads()
{
    Fixture fixture({"documents"});
    fixture.note("note.md", "old private body");
    const Json arguments{{"collection", "notes"}, {"filename", "note.md"}};
    expect(fixture.call("documents_read", arguments)["text"] == "old private body", "first live read must return source text");
    fixture.note("note.md", "current private body");
    expect(fixture.call("documents_read", arguments)["text"] == "current private body", "a later read must not return cached old contents");
    expect(chmod((fixture.notes() / "note.md").c_str(), 0000) == 0, "synthetic revocation must succeed");
    expect_error(
        [&]
        {
            fixture.call("documents_read", arguments);
        },
        "revoked files must not disclose cached contents");
    expect(chmod((fixture.notes() / "note.md").c_str(), 0600) == 0, "synthetic source must be restored");
    expect(chmod(fixture.notes().c_str(), 0750) == 0, "synthetic root revocation must succeed");
    expect_error(
        [&]
        {
            fixture.call("documents_read", arguments);
        },
        "revoked roots must not disclose previous contents");
    expect(chmod(fixture.notes().c_str(), 0700) == 0, "synthetic root must be restored");

    fixture.on_clock = [&]
    {
        expect(chmod((fixture.notes() / "note.md").c_str(), 0000) == 0, "in-call revocation must succeed");
    };
    expect_error(
        [&]
        {
            fixture.call("documents_read", arguments);
        },
        "revocation observed after the first read must suppress the accumulated page");
    expect(chmod((fixture.notes() / "note.md").c_str(), 0600) == 0, "synthetic source must be restored again");
    fixture.on_clock = [&]
    {
        fixture.config["connectors"]["documents"]["collections"] = Json::array();
    };
    expect_error(
        [&]
        {
            fixture.call("documents_search", {{"collection", "notes"}, {"query", "private"}});
        },
        "in-call allowlist revocation must suppress accumulated snippets");
    fixture.config["connectors"]["documents"]["collections"] = {{{"name", "notes"}, {"path", fixture.notes().string()}}};
    std::filesystem::remove(fixture.notes() / "note.md");
    expect_error(
        [&]
        {
            fixture.call("documents_read", arguments);
        },
        "deleted files must not fall back to cached text");
    fixture.config["switchboard"]["connectors"] = Json::array();
    expect_error(
        [&]
        {
            fixture.call("documents_collections");
        },
        "disabled document access must fail immediately", "documents_disabled");
}

void test_task_limits_and_strict_instants()
{
    Fixture fixture({"tasks"});
    const auto item = fixture.call("tasks_add", {{"title", "  bounded task  "}, {"due_at", "2023-11-14T14:13:20-08:00"}, {"remind_at", due_now}});
    expect(item["title"] == "bounded task" && item["due_at"] == due_now && item["remind_at"] == due_now && item["revision"] == 1 && item["status"] == "open" &&
               item["list_name"] == "personal" && item["notes"] == "",
           "task defaults and explicit-offset normalization must be deterministic");
    for (const auto &instant : std::vector<std::string>{
             "2023-11-14", "2023-11-14T22:13:20", "2023-11-14 22:13:20Z", "2023-11-14t22:13:20Z", "2023-11-14T22:13:20z", "2023-11-14T22:13:20.1Z",
             "2023-02-29T22:13:20Z", "2024-02-30T22:13:20Z", "2023-13-14T22:13:20Z", "2023-11-14T24:13:20Z", "2023-11-14T22:60:20Z", "2023-11-14T22:13:60Z",
             "2023-11-14T22:13:20+24:00", "2023-11-14T22:13:20+00:60", "2023-11-14T22:13:20-00:00", "0000-01-01T00:00:00Z", "1969-12-31T23:59:59Z",
             "1970-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"})
    {
        expect_error(
            [&]
            {
                fixture.call("tasks_add", {{"title", "invalid instant"}, {"remind_at", instant}});
            },
            "ambiguous, malformed, impossible, and out-of-range instants must be rejected");
    }
    const auto leap = fixture.call("tasks_add", {{"title", "leap day"}, {"due_at", "2024-02-29T12:00:00+05:30"}});
    expect(leap["due_at"] == "2024-02-29T06:30:00Z", "real leap dates and numeric offsets must be supported");
    std::string wide_title;
    for (int index = 0; index < 51; ++index)
    {
        wide_title += "\xF0\x9F\x98\x80";
    }
    for (const auto &arguments : std::vector<Json>{{{"title", ""}},
                                                   {{"title", "   "}},
                                                   {{"title", "two\nlines"}},
                                                   {{"title", std::string(201, 'x')}},
                                                   {{"title", wide_title}},
                                                   {{"title", "ok"}, {"notes", std::string(4001, 'x')}},
                                                   {{"title", "ok"}, {"notes", "\xC2\x85"}},
                                                   {{"title", "ok"}, {"notes", nullptr}},
                                                   {{"title", "ok"}, {"list_name", std::string(65, 'x')}},
                                                   {{"title", "ok"}, {"due_at", true}},
                                                   {{"title", "ok"}, {"recurrence", "daily"}},
                                                   {{"title", "ok"}, {"status", "completed"}}})
    {
        expect_error(
            [&]
            {
                fixture.call("tasks_add", arguments);
            },
            "task bounds and unsupported fields must fail");
    }
    expect_error(
        [&]
        {
            fixture.call("tasks_update", {{"id", item["id"]}, {"revision", item["revision"]}});
        },
        "an empty update must not increment the revision");
    expect(fixture.call("tasks_status")["counts"]["open"] == 2, "invalid requests must not create tasks");
}

void test_task_paging_search_and_revision_conflicts()
{
    Fixture fixture({"tasks"});
    auto first = fixture.call("tasks_add", {{"title", "A.* [x]"},
                                            {"notes", std::string(179, 'a') + "\xC3\xA9"
                                                                              " tail"},
                                            {"due_at", due_now}});
    const auto second = fixture.call("tasks_add", {{"title", "alpha"}, {"list_name", "work"}, {"due_at", due_now}});
    fixture.call("tasks_add", {{"title", "ALPHA"}});
    const auto page = fixture.call("tasks_list", {{"limit", 1}});
    expect(page["truncated"] == true && page["next_after_id"] == first["id"] && page["tasks"][0]["notes_truncated"] == true &&
               page["tasks"][0]["notes"].get<std::string>().size() == 179,
           "task previews must be byte-bounded, preserve encoding, and expose paging");
    const auto later = fixture.call("tasks_list", {{"after_id", page["next_after_id"]}, {"limit", 1}});
    expect(later["tasks"][0]["id"] == second["id"], "task paging must use increasing identifiers");
    expect(fixture.call("tasks_list", {{"query", ".* [x]"}})["tasks"].size() == 1 && fixture.call("tasks_list", {{"query", "alpha"}})["tasks"].size() == 1 &&
               fixture.call("tasks_list", {{"query", "a.*"}})["tasks"].empty(),
           "task search must be literal and case-sensitive, not pattern-based");
    expect(fixture.call("tasks_list", {{"list_name", "work"}, {"overdue", true}})["tasks"].size() == 1, "list and overdue filters must be applied together");
    const auto original = first;
    first = edit(fixture, first, {{"title", "edited"}});
    expect(first["revision"] == 2, "successful edits must increment revisions");
    expect_error(
        [&]
        {
            edit(fixture, original, {{"notes", "stale overwrite"}});
        },
        "stale edits must fail", "tasks_conflict");
    expect_error(
        [&]
        {
            fixture.call("tasks_delete", {{"id", original["id"]}, {"revision", original["revision"]}});
        },
        "stale deletion must fail", "tasks_conflict");
    expect(fixture.call("tasks_read", {{"id", first["id"]}})["notes"] == original["notes"], "failed stale changes must preserve current contents");
    first = edit(fixture, first, {{"status", "completed"}});
    expect(fixture.call("tasks_list", {{"overdue", true}})["tasks"].size() == 1 &&
               fixture.call("tasks_list", {{"status", "completed"}})["tasks"][0]["id"] == first["id"],
           "completed tasks must not count as open overdue work");
    expect_error(
        [&]
        {
            fixture.call("tasks_read", {{"id", MAX_JSON_INTEGER}});
        },
        "missing tasks must fail visibly", "tasks_not_found");
}

void test_task_persistence_and_increasing_ids()
{
    Fixture fixture({"tasks"});
    auto first = fixture.call("tasks_add", {{"title", "retained"}, {"notes", "bounded persistent notes"}});
    const auto second = fixture.call("tasks_add", {{"title", "remove me"}});
    fixture.call("tasks_delete", {{"id", second["id"]}, {"revision", second["revision"]}});
    const auto third = fixture.call("tasks_add", {{"title", "new identifier"}});
    expect(third["id"].get<std::int64_t>() > second["id"].get<std::int64_t>(),
           "committed identifiers must not be reused after deleting the greatest identifier");
    first = edit(fixture, first, {{"notes", "updated persistent notes"}});
    Database reopened(fixture.directory.path / "personal-test.sqlite3");
    Context context(fixture.config, fixture.directory.path, reopened, fixture.http,
                    [&]
                    {
                        return fixture.time;
                    });
    Registry registry;
    register_personal(context, registry);
    const auto restored = registry.find("tasks_read")->handler({{"id", first["id"]}});
    expect(restored["revision"] == first["revision"] && restored["notes"] == first["notes"],
           "re-registration on a reopened native database must retain tasks and revisions");
    expect(fixture.http.requests.empty(), "persistence must not use a task provider");
}

void test_reminder_idempotency_and_schedule_changes()
{
    Fixture fixture({"tasks"});
    auto item = fixture.call("tasks_add", {{"title", "reminder title"}, {"remind_at", due_now}});
    fixture.poll();
    fixture.poll();
    item = fixture.call("tasks_read", {{"id", item["id"]}});
    expect(fixture.event_count("tasks.reminder_due") == 1 && item["revision"] == 1 && item["reminder_event_queued_at"] == due_now &&
               item["reminder_delivery"] == "event_queued",
           "repeated polls must queue one event and not change the edit revision");
    item = edit(fixture, item, {{"title", "new title"}, {"notes", "not a new schedule"}, {"list_name", "work"}, {"due_at", timestamp(base_time + 600)}});
    fixture.poll();
    item = edit(fixture, item, {{"status", "completed"}});
    fixture.poll();
    item = edit(fixture, item, {{"status", "open"}});
    item = edit(fixture, item, {{"remind_at", "2023-11-14T14:13:20-08:00"}});
    fixture.poll();
    expect(fixture.event_count("tasks.reminder_due") == 1, "unrelated edits and equivalent instants must not resend reminders");
    item = edit(fixture, item, {{"remind_at", timestamp(base_time + 1)}});
    expect(item["reminder_event_queued_at"].is_null(), "a different reminder instant must reset its marker");
    fixture.time = base_time + 1;
    fixture.poll();
    item = fixture.call("tasks_read", {{"id", item["id"]}});
    expect(fixture.event_count("tasks.reminder_due") == 2, "a different schedule must queue its own one-shot event");
    item = edit(fixture, item, {{"remind_at", due_now}});
    fixture.poll();
    expect(fixture.event_count("tasks.reminder_due") == 3,
           "explicitly returning to an older instant must create a new generation, not collide with its old dedupe key");
    item = fixture.call("tasks_read", {{"id", item["id"]}});
    item = edit(fixture, item, {{"remind_at", nullptr}});
    fixture.poll();
    expect(item["reminder_delivery"] == "not_scheduled" && fixture.event_count("tasks.reminder_due") == 3,
           "clearing a schedule must cancel future queueing without withdrawing prior events");
    const auto events = fixture.database.query("SELECT payload, dedupe_key FROM native_events WHERE type = ? ORDER BY id", {"tasks.reminder_due"});
    const auto payload = parse_json(events[1]["payload"].get<std::string>());
    expect(payload["id"] == item["id"] && payload["title"] == "new title" && payload["untrusted_content"] == true && payload["delivery"] == "event_queued" &&
               events[0]["dedupe_key"] != events[1]["dedupe_key"],
           "reminder payloads must be bounded untrusted observations, not delivery receipts");
    fixture.call("tasks_delete", {{"id", item["id"]}, {"revision", item["revision"]}});
    expect(fixture.event_count("tasks.reminder_due") == 3 && fixture.event_count("tasks.deleted") == 1,
           "deleting a task must not pretend to retract queued reminders");
}

void test_reminder_cancellation_completion_and_batching()
{
    Fixture fixture({"tasks"}, Json::object(), {{"poll_interval_seconds", 30}, {"max_reminders_per_tick", 1}});
    auto cancelled = fixture.call("tasks_add", {{"title", "cancelled"}, {"remind_at", due_now}});
    auto completed = fixture.call("tasks_add", {{"title", "completed"}, {"remind_at", due_now}});
    fixture.call("tasks_add", {{"title", "first open"}, {"remind_at", due_now}});
    fixture.call("tasks_add", {{"title", "second open"}, {"remind_at", due_now}});
    auto future = fixture.call("tasks_add", {{"title", "future"}, {"remind_at", timestamp(base_time + 60)}});
    cancelled = edit(fixture, cancelled, {{"status", "cancelled"}});
    completed = edit(fixture, completed, {{"status", "completed"}});
    fixture.poll();
    expect(fixture.event_count("tasks.reminder_due") == 1, "one poll must respect its bounded batch size");
    fixture.poll();
    fixture.poll();
    const auto status = fixture.call("tasks_status");
    expect(fixture.event_count("tasks.reminder_due") == 2 && status["last_check"]["queued"] == 0 && status["last_check_is_liveness"] == false &&
               status["counts"]["cancelled"] == 1 && status["counts"]["completed"] == 1 && status["poll_interval_seconds"] == 30,
           "inactive and future tasks must not queue while an open backlog drains across polls");
    future = edit(fixture, future, {{"remind_at", nullptr}});
    completed = edit(fixture, completed, {{"status", "open"}});
    fixture.time += 60;
    fixture.poll();
    expect(fixture.event_count("tasks.reminder_due") == 3, "reopening a previously unqueued overdue schedule must queue it, but a cleared schedule must not");
    fixture.config["switchboard"]["connectors"] = Json::array();
    fixture.poll();
    expect_error(
        [&]
        {
            fixture.call("tasks_read", {{"id", cancelled["id"]}});
        },
        "in-memory disabling must revoke task tools", "tasks_disabled");
    expect(fixture.event_count("tasks.reminder_due") == 3, "disabled polling must have no queueing effect");
}

void test_reminder_and_mutation_rollback()
{
    Fixture fixture({"tasks"});
    auto first = fixture.call("tasks_add", {{"title", "first"}, {"notes", "retained"}, {"remind_at", due_now}});
    const auto second = fixture.call("tasks_add", {{"title", "second"}, {"remind_at", due_now}});
    fixture.database.execute("CREATE TRIGGER native_tasks_test_fail_second_reminder BEFORE INSERT ON native_events "
                             "WHEN NEW.source = 'tasks' AND NEW.type = 'tasks.reminder_due' "
                             "AND EXISTS (SELECT 1 FROM native_events WHERE source = 'tasks' AND type = 'tasks.reminder_due') "
                             "BEGIN SELECT RAISE(ABORT, 'synthetic reminder failure'); END");
    expect_error(
        [&]
        {
            fixture.poll();
        },
        "a second-event failure must abort the complete reminder batch");
    expect(fixture.event_count("tasks.reminder_due") == 0 && fixture.call("tasks_read", {{"id", first["id"]}})["reminder_event_queued_at"].is_null() &&
               fixture.call("tasks_read", {{"id", second["id"]}})["reminder_event_queued_at"].is_null() && fixture.call("tasks_status")["last_check"].is_null(),
           "queue events, both markers, and successful-check state must roll back together");
    fixture.database.execute("DROP TRIGGER native_tasks_test_fail_second_reminder");
    fixture.poll();
    fixture.poll();
    expect(fixture.event_count("tasks.reminder_due") == 2, "a retried rolled-back batch must queue once per task");
    fixture.database.execute("CREATE TRIGGER native_tasks_test_fail_change BEFORE INSERT ON native_events "
                             "WHEN NEW.source = 'tasks' AND NEW.type = 'tasks.changed' "
                             "BEGIN SELECT RAISE(ABORT, 'synthetic mutation failure'); END");
    expect_error(
        [&]
        {
            fixture.call("tasks_add", {{"title", "must roll back"}});
        },
        "creation must roll back if its event cannot be queued");
    expect_error(
        [&]
        {
            edit(fixture, first, {{"notes", "must roll back"}});
        },
        "editing must roll back if its event cannot be queued");
    const auto retained = fixture.call("tasks_read", {{"id", first["id"]}});
    expect(retained["notes"] == "retained" && retained["revision"] == 1 && fixture.call("tasks_status")["counts"]["open"] == 2,
           "failed mutations must preserve content, revisions, and row counts");
    fixture.database.execute("DROP TRIGGER native_tasks_test_fail_change");
    fixture.database.execute("CREATE TRIGGER native_tasks_test_fail_delete BEFORE INSERT ON native_events "
                             "WHEN NEW.source = 'tasks' AND NEW.type = 'tasks.deleted' "
                             "BEGIN SELECT RAISE(ABORT, 'synthetic deletion failure'); END");
    expect_error(
        [&]
        {
            fixture.call("tasks_delete", {{"id", first["id"]}, {"revision", first["revision"]}});
        },
        "deletion and its event must roll back together");
    expect(fixture.call("tasks_read", {{"id", first["id"]}})["revision"] == 1 && fixture.event_count("tasks.deleted") == 0,
           "failed deletion must retain the exact task");
}

void test_runtime_exact_revision_deletion_approval()
{
    Workspace directory;
    auto transport = std::make_unique<FakeHttp>();
    auto *http = transport.get();
    Runtime runtime(configuration(directory, {"tasks"}), std::move(transport),
                    []
                    {
                        return base_time;
                    });
    const auto created = runtime.call("tasks_add", {{"title", "approval target"}});
    expect(created["status"] == "ok", "draft-tier local creation must not require action approval by default");
    auto item = created["data"];
    const Json old_arguments{{"id", item["id"]}, {"revision", item["revision"]}};
    const auto pending = runtime.call("tasks_delete", old_arguments);
    expect(pending["status"] == "pending" && runtime.call("tasks_read", {{"id", item["id"]}})["status"] == "ok",
           "assistant deletion must ask before running its handler");
    Database inspect(directory.path / "native-state.sqlite3");
    const auto saved = inspect.query("SELECT args, summary FROM native_approvals WHERE id = ?", {pending["approval_id"]});
    expect(saved.size() == 1 && parse_json(saved.front()["args"].get<std::string>()) == old_arguments &&
               saved.front()["summary"].get<std::string>().find("revision 1") != std::string::npos,
           "approval must persist the exact identifier and revision, not a broad deletion permission");
    const auto edited = runtime.call("tasks_update", {{"id", item["id"]}, {"revision", item["revision"]}, {"title", "newer revision"}});
    expect(edited["status"] == "ok", "a newer edit must be possible before pending approval is claimed");
    item = edited["data"];
    const auto stale = runtime.approve(pending["approval_id"].get<std::string>());
    expect(stale["status"] == "error" && runtime.call("tasks_read", {{"id", item["id"]}})["data"]["revision"] == item["revision"],
           "approving an old deletion must not delete the later task revision");
    expect(runtime.approve(pending["approval_id"].get<std::string>())["status"] != "ok", "a claimed stale approval must not become replayable permission");
    expect(runtime.call("tasks_delete", {{"id", item["id"]}, {"revision", item["revision"]}, {"all", true}})["status"] == "invalid",
           "unknown deletion fields must fail before an approval is created");
    const auto other = runtime.call("tasks_add", {{"title", "unrelated retained task"}})["data"];
    const auto exact = runtime.call("tasks_delete", {{"id", item["id"]}, {"revision", item["revision"]}});
    expect(exact["status"] == "pending", "current-revision deletion must still require approval");
    const auto deleted = runtime.approve(exact["approval_id"].get<std::string>());
    expect(deleted["status"] == "ok" && deleted["data"]["id"] == item["id"] && deleted["data"]["revision"] == item["revision"] &&
               runtime.call("tasks_read", {{"id", item["id"]}})["status"] == "error" && runtime.call("tasks_read", {{"id", other["id"]}})["status"] == "ok",
           "exact approval must delete only its recorded task revision");
    expect(runtime.approve(exact["approval_id"].get<std::string>())["status"] != "ok", "successful deletion approval must be one-shot");
    expect(http->requests.empty(), "approval, edits, and deletion must not attempt any network request");
}
}  // namespace

int main()
{
    try
    {
        test_independent_registration_and_no_scan();
        test_configuration_rejection();
        test_document_paths_links_and_private_modes();
        test_document_bounds_paging_and_invalid_text();
        test_document_literal_search_and_budget();
        test_document_revocation_and_live_reads();
        test_task_limits_and_strict_instants();
        test_task_paging_search_and_revision_conflicts();
        test_task_persistence_and_increasing_ids();
        test_reminder_idempotency_and_schedule_changes();
        test_reminder_cancellation_completion_and_batching();
        test_reminder_and_mutation_rollback();
        test_runtime_exact_revision_deletion_approval();
        std::cout << "native personal tests passed\n";
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "native personal test failure: " << error.what() << '\n';
        return 1;
    }
}
