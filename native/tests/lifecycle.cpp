#include "support.hpp"

#include <fstream>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <sys/stat.h>

using namespace switchboard;
using switchboard::test::Directory;
using switchboard::test::expect;
using switchboard::test::expect_error_code;
using switchboard::test::FakeHttp;
using switchboard::test::ManualClock;
using switchboard::test::SyntheticProbe;

namespace
{
std::filesystem::path state_path(const Directory &directory)
{
    return directory.path / "native-state.sqlite3";
}

bool refused_with(const std::function<void()> &open, const std::string &text)
{
    try
    {
        open();
    }
    catch (const Error &error)
    {
        return error.code() == "database" && std::string(error.what()).find(text) != std::string::npos;
    }
    return false;
}

// Rebuilds the file with the version 1 schema statements of the previous engine, plus legacy rows.
void write_version_one(const std::filesystem::path &path, const std::int64_t now)
{
    Database raw(path);
    for (const auto *table : {"native_actions", "native_audit", "native_events", "native_calls", "native_approvals"})
    {
        raw.execute(std::string("DROP TABLE ") + table);
    }
    raw.execute("CREATE TABLE IF NOT EXISTS native_approvals (id TEXT PRIMARY KEY, tool TEXT NOT NULL, args TEXT NOT NULL, summary TEXT NOT NULL, "
                "status TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, result TEXT, error TEXT)");
    raw.execute("CREATE TABLE IF NOT EXISTS native_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT NOT NULL, state TEXT NOT NULL, "
                "created_at INTEGER NOT NULL)");
    raw.execute("CREATE INDEX IF NOT EXISTS native_call_budget ON native_calls(tool, state, created_at)");
    raw.execute("CREATE TABLE IF NOT EXISTS native_events (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, type TEXT NOT NULL, "
                "payload TEXT NOT NULL, dedupe_key TEXT UNIQUE, created_at INTEGER NOT NULL)");
    raw.execute("CREATE TABLE IF NOT EXISTS native_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, previous_hash TEXT NOT NULL, record TEXT NOT NULL, "
                "hash TEXT NOT NULL)");
    raw.execute("PRAGMA user_version = 1");
    raw.execute("INSERT INTO native_approvals(id, tool, args, summary, status, created_at, expires_at, result, error) "
                "VALUES (?, 'switchboard_status', '{}', 'legacy', 'executed', ?, ?, '{}', NULL)",
                {std::string(32, '0'), now - 100, now + 3500});
    raw.execute("INSERT INTO native_events(source, type, payload, dedupe_key, created_at) VALUES ('legacy', 'legacy.event', '{}', NULL, ?)", {now});
    const Json record = {{"event", "legacy"}};
    raw.execute("INSERT INTO native_audit(previous_hash, record, hash) VALUES ('', ?, ?)", {record.dump(), hash(Json{{"previous", ""}, {"record", record}})});
}

void rewrite_manifest(const std::filesystem::path &directory, const std::function<void(Json &)> &change)
{
    auto manifest = parse_json(read_private_file(directory, "manifest.json", 65536, true));
    change(manifest);
    std::ofstream output(directory / "manifest.json", std::ios::binary | std::ios::trunc);
    output << manifest.dump(2) << '\n';
    output.flush();
    expect(static_cast<bool>(output), "the test manifest could not be rewritten");
}

std::uint32_t mode_of(const std::filesystem::path &path)
{
    struct stat info
    {
    };
    expect(lstat(path.c_str(), &info) == 0, "a backup entry is missing");
    return info.st_mode & 0777;
}

void migrates_version_one()
{
    Directory directory;
    ManualClock clock;
    write_version_one(state_path(directory), clock.now());
    SyntheticProbe probe;
    Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>(), clock.clock(), test::synthetic_tools(probe));
    Database inspect(state_path(directory));
    expect(inspect.query("PRAGMA user_version").front()["user_version"] == STATE_SCHEMA_VERSION, "version 1 state must migrate to the current version");
    expect(inspect.query("SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'native_actions_%'").size() == 3,
           "the migration must create the journal indexes");
    expect(inspect.query("SELECT id FROM native_approvals").size() == 1 && inspect.query("SELECT id FROM native_events").size() == 1,
           "the migration must preserve existing rows");
    const auto audit = runtime.call("audit_verify", Json::object());
    expect(audit["data"]["chain_matches"] == true && audit["data"]["records"].get<std::int64_t>() >= 2, "the migrated audit chain must still verify");
    expect(runtime.call("synthetic_write", {{"mode", "ok"}})["action"]["state"] == "succeeded", "a migrated state must journal new attempts");
    expect_error_code(
        [&]
        {
            inspect.execute("INSERT INTO native_actions(id, tool, args_digest, args, origin, state, owner, started_at, deadline_at) "
                            "VALUES ('NOT-HEX', 'synthetic_write', ?, '{}', 'assistant', 'in_flight', 'test', 1, 2)",
                            {std::string(64, 'a')});
        },
        "database", "the journal must reject malformed identifiers");
    expect_error_code(
        [&]
        {
            inspect.execute("INSERT INTO native_actions(id, tool, args_digest, args, origin, state, owner, started_at, deadline_at) "
                            "VALUES (?, 'synthetic_write', ?, '{}', 'assistant', 'done', 'test', 1, 2)",
                            {std::string(32, 'a'), std::string(64, 'a')});
        },
        "database", "the journal must reject unknown states");
}

void refuses_newer_and_incomplete_state()
{
    {
        Directory directory;
        {
            Database created(state_path(directory));
            created.execute("PRAGMA user_version = 3");
        }
        expect(refused_with(
                   [&]
                   {
                       Database reopened(state_path(directory));
                   },
                   "newer"),
               "state written by a newer schema must be refused, not downgraded");
        expect_error_code(
            [&]
            {
                Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>());
            },
            "database", "a runtime must not start on newer state");
    }
    {
        Directory directory;
        {
            Database created(state_path(directory));
            created.execute("DROP TABLE native_actions");
        }
        expect(refused_with(
                   [&]
                   {
                       Database reopened(state_path(directory));
                   },
                   "incomplete"),
               "a version 2 state without its journal must be refused");
    }
    {
        Directory directory;
        {
            Database created(state_path(directory));
            created.execute("PRAGMA user_version = 1");
        }
        expect(refused_with(
                   [&]
                   {
                       Database reopened(state_path(directory));
                   },
                   "already contains"),
               "a version 1 state that already has a journal must be refused");
    }
    {
        Directory directory;
        {
            Database created(state_path(directory));
            created.execute("DROP TABLE native_audit");
            created.execute("DROP TABLE native_actions");
            created.execute("PRAGMA user_version = 1");
        }
        expect(refused_with(
                   [&]
                   {
                       Database reopened(state_path(directory));
                   },
                   "incomplete"),
               "an incomplete version 1 state must be refused rather than recreated");
    }
}

void backup_verify_and_tamper()
{
    Directory directory;
    ManualClock clock;
    SyntheticProbe probe;
    Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>(), clock.clock(), test::synthetic_tools(probe));
    expect(runtime.call("synthetic_write", {{"mode", "ok"}})["status"] == "ok", "the backup fixture write failed");
    expect(runtime.call("synthetic_write", {{"mode", "effect_unknown"}})["action"]["state"] == "uncertain", "the backup fixture needs an open entry");
    Directory backups;
    const auto target = backups.path / "first";
    const auto created = runtime.backup(target);
    expect(created["status"] == "ok" && created["schema_version"] == STATE_SCHEMA_VERSION && created["sha256"].get<std::string>().size() == 64 &&
               created["audit_records"].get<std::int64_t>() > 0 && created["external_state_verified"] == false,
           "a backup must report its manifest");
    expect(mode_of(target) == 0700 && mode_of(target / "native-state.sqlite3") == 0600 && mode_of(target / "manifest.json") == 0600,
           "backup directories and files must be private");
    const auto verified = Runtime::verify_backup(target);
    expect(verified["status"] == "ok" && verified["verified"] == true && verified["audit_head"] == created["audit_head"] &&
               verified["audit_records"] == created["audit_records"] && verified["sha256"] == created["sha256"] && verified["external_state_verified"] == false,
           "a fresh backup must verify");
    {
        Database copy(target / "native-state.sqlite3", DatabaseMode::read_only);
        const auto last = copy.query("SELECT hash FROM native_audit ORDER BY id DESC LIMIT 1");
        expect(!last.empty() && last.front()["hash"] == created["audit_head"], "the manifest head must be the copy's last audit hash");
        expect(copy.query("SELECT id FROM native_actions").size() == 2, "the copy must contain the journal");
        expect_error_code(
            [&]
            {
                copy.execute("DELETE FROM native_actions");
            },
            "database", "a read-only copy must refuse writes");
    }
    expect(Runtime::verify_backup(target)["verified"] == true, "read-only inspection must not change the copy");
    Database live(state_path(directory));
    expect(!live.query("SELECT id FROM native_audit WHERE record LIKE '%state.backup%'").empty(), "backups must be audited");

    expect(runtime.backup(target)["status"] == "error", "a backup must not overwrite an existing one");
    const auto loose = backups.path / "loose";
    std::filesystem::create_directory(loose);
    std::filesystem::permissions(loose, static_cast<std::filesystem::perms>(0755));
    expect(runtime.backup(loose)["status"] == "error", "a backup directory readable by others must be refused");
    expect(runtime.backup("relative/backup")["status"] == "error", "a relative backup path must be refused");
    expect(runtime.backup(backups.path / "missing-parent" / "child")["status"] == "error", "a backup must not create missing parents");

    const auto flipped = backups.path / "flipped";
    expect(runtime.backup(flipped)["status"] == "ok", "the tamper fixture backup failed");
    {
        std::fstream file(flipped / "native-state.sqlite3", std::ios::in | std::ios::out | std::ios::binary);
        file.seekg(-1, std::ios::end);
        char last = 0;
        file.get(last);
        file.seekp(-1, std::ios::end);
        file.put(static_cast<char>(last ^ 0x01));
        expect(static_cast<bool>(file), "the test byte flip failed");
    }
    const auto changed = Runtime::verify_backup(flipped);
    expect(changed["verified"] == false && changed["status"] == "error" && changed["message"].get<std::string>().find("digest") != std::string::npos,
           "a changed backup byte must fail verification");

    const auto moved = backups.path / "moved-head";
    expect(runtime.backup(moved)["status"] == "ok", "the head fixture backup failed");
    rewrite_manifest(moved,
                     [](Json &manifest)
                     {
                         manifest["audit_head"] = std::string(64, '0');
                     });
    const auto wrong_head = Runtime::verify_backup(moved);
    expect(wrong_head["verified"] == false && wrong_head["message"].get<std::string>().find("audit head") != std::string::npos,
           "a manifest head that differs from the copy must fail verification");

    const auto forged = backups.path / "forged";
    expect(runtime.backup(forged)["status"] == "ok", "the forged fixture backup failed");
    {
        Database copy(forged / "native-state.sqlite3");
        copy.execute("UPDATE native_audit SET record = '{\"event\":\"forged\"}' WHERE id = 1");
        copy.execute("PRAGMA journal_mode = DELETE");
    }
    const auto digest = sha256_file(forged, "native-state.sqlite3", 1ULL << 32);
    rewrite_manifest(forged,
                     [&](Json &manifest)
                     {
                         manifest["sha256"] = digest;
                     });
    const auto rehashed = Runtime::verify_backup(forged);
    expect(rehashed["verified"] == false && rehashed["message"].get<std::string>().find("audit chain") != std::string::npos,
           "an edited audit record must fail verification even with a matching digest");
    std::filesystem::remove(forged / "manifest.json");
    expect(Runtime::verify_backup(forged)["verified"] == false, "a backup without its manifest must not verify");
}

void purge_keeps_open_entries()
{
    Directory directory;
    ManualClock clock;
    auto configuration = test::configuration(directory, {});
    configuration["retention"] = {{"tool_calls_days", 1}, {"approvals_days", 1}, {"actions_days", 1}};
    configuration["switchboard"]["event_retention_days"] = 1;
    Runtime runtime(configuration, std::make_unique<FakeHttp>(), clock.clock());
    const auto now = clock.now();
    const auto old = now - 10 * 86400;
    {
        Database other(state_path(directory));
        const auto action = [&](const char letter, const std::string &state, const Json &finished, const Json &resolved)
        {
            const std::string id(32, letter);
            other.execute("INSERT INTO native_actions(id, tool, args_digest, args, origin, approval_id, state, owner, started_at, deadline_at, "
                          "finished_at, resolved_at) VALUES (?, 'synthetic_write', ?, '{}', 'assistant', NULL, ?, 'test', ?, ?, ?, ?)",
                          {id, hash(Json{{"id", id}}), state, old, old + 120, finished, resolved});
        };
        action('a', "succeeded", old, nullptr);
        action('b', "failed", old, nullptr);
        action('c', "reconciled_applied", old, old + 10);
        action('d', "reconciled_not_applied", old, now - 3600);
        action('e', "in_flight", nullptr, nullptr);
        action('f', "uncertain", old, nullptr);
        const auto approval = [&](const char digit, const std::string &status, const std::int64_t created)
        {
            other.execute(
                "INSERT INTO native_approvals(id, tool, args, summary, status, created_at, expires_at) VALUES (?, 'synthetic_act', '{}', 'test', ?, ?, ?)",
                {std::string(32, digit), status, created, created + 3600});
        };
        approval('1', "executed", old);
        approval('2', "denied", old);
        approval('3', "pending", old);
        approval('4', "running", old);
        approval('5', "pending", now);
        other.execute("INSERT INTO native_calls(tool, state, created_at) VALUES ('synthetic_write', 'ok', ?)", {old});
        other.execute("INSERT INTO native_calls(tool, state, created_at) VALUES ('synthetic_write', 'ok', ?)", {now});
        other.execute("INSERT INTO native_events(source, type, payload, dedupe_key, created_at) VALUES ('test', 'old', '{\"a\":1}', NULL, ?)", {old});
        other.execute("INSERT INTO native_events(source, type, payload, dedupe_key, created_at) VALUES ('test', 'kept-key', '{\"a\":1}', 'key-1', ?)", {old});
        other.execute("INSERT INTO native_events(source, type, payload, dedupe_key, created_at) VALUES ('test', 'recent', '{\"a\":1}', NULL, ?)", {now});
    }
    const Json expected = {{"tool_calls", 1}, {"approvals", 3}, {"actions", 3}, {"events", 1}, {"event_payloads", 1}};
    const auto preview = runtime.purge(true);
    expect(preview["status"] == "ok" && preview["dry_run"] == true && preview["counts"] == expected && preview["cutoffs"]["actions"].is_string(),
           "a dry run must count what retention would remove");
    Database inspect(state_path(directory));
    expect(inspect.query("SELECT id FROM native_actions").size() == 6 && inspect.query("SELECT id FROM native_approvals").size() == 5 &&
               inspect.query("SELECT id FROM native_calls").size() == 2 && inspect.query("SELECT id FROM native_events").size() == 3,
           "a dry run must not change state");
    const auto purged = runtime.purge(false);
    expect(purged["status"] == "ok" && purged["dry_run"] == false && purged["counts"] == expected, "an applied purge must match its preview");
    const auto actions = inspect.query("SELECT state FROM native_actions ORDER BY id");
    expect(actions.size() == 3 && actions[0]["state"] == "reconciled_not_applied" && actions[1]["state"] == "in_flight" && actions[2]["state"] == "uncertain",
           "purge must keep open journal entries and recently resolved ones");
    const auto approvals = inspect.query("SELECT status FROM native_approvals ORDER BY id");
    expect(approvals.size() == 2 && approvals[0]["status"] == "running" && approvals[1]["status"] == "pending",
           "purge must keep running and live pending approvals");
    expect(inspect.query("SELECT payload FROM native_events WHERE dedupe_key = 'key-1'").front()["payload"] == "{}" &&
               inspect.query("SELECT id FROM native_events").size() == 2,
           "event retention must delete old events and prune old deduplicated payloads");
    const Json nothing = {{"tool_calls", 0}, {"approvals", 0}, {"actions", 0}, {"events", 0}, {"event_payloads", 0}};
    expect(runtime.purge(false)["counts"] == nothing, "a repeated purge must find nothing more to remove");
    expect(inspect.query("SELECT id FROM native_audit WHERE record LIKE '%state.purge%'").size() == 2, "each applied purge must be audited");
}

void kill_switch_control()
{
    Directory directory;
    SyntheticProbe probe;
    Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>(), {}, test::synthetic_tools(probe));
    const auto marker = directory.path / "KILL_SWITCH";
    const auto initial = runtime.kill_switch(std::nullopt);
    expect(initial["status"] == "ok" && initial["kill_switch"] == false && initial["changed"] == false, "the kill switch must start released");
    const auto engaged = runtime.kill_switch(true);
    expect(engaged["kill_switch"] == true && engaged["changed"] == true && std::filesystem::exists(marker) && mode_of(marker) == 0600,
           "engaging must create a private KILL_SWITCH file");
    expect(runtime.kill_switch(true)["changed"] == false, "engaging twice must report no change");
    expect(runtime.call("synthetic_write", {{"mode", "ok"}}, Origin::human)["status"] == "denied" && probe.calls == 0, "the kill switch must deny writes");
    expect(runtime.call("synthetic_read", Json::object())["status"] == "ok", "the kill switch must not stop reads");
    expect(runtime.call("switchboard_status", Json::object())["data"]["kill_switch"] == true, "status must report the kill switch");
    const auto released = runtime.kill_switch(false);
    expect(released["kill_switch"] == false && released["changed"] == true && !std::filesystem::exists(marker), "releasing must remove the file");
    expect(runtime.kill_switch(false)["changed"] == false, "releasing twice must report no change");
    expect(runtime.call("synthetic_write", {{"mode", "ok"}}, Origin::human)["status"] == "ok", "writes must resume after release");
    std::filesystem::create_symlink(directory.path / "missing-target", marker);
    expect(runtime.kill_switch(std::nullopt)["kill_switch"] == true, "any KILL_SWITCH entry, even a dangling link, must engage the switch");
    expect(runtime.kill_switch(false)["changed"] == true && runtime.kill_switch(std::nullopt)["kill_switch"] == false, "releasing must remove a link entry");
    Database inspect(state_path(directory));
    expect(inspect.query("SELECT id FROM native_audit WHERE record LIKE '%kill_switch.on%'").size() == 2 &&
               inspect.query("SELECT id FROM native_audit WHERE record LIKE '%kill_switch.off%'").size() == 3,
           "every requested kill-switch change must be audited; status reads must not be");
}
}  // namespace

int main()
{
    try
    {
        migrates_version_one();
        refuses_newer_and_incomplete_state();
        backup_verify_and_tamper();
        purge_keeps_open_entries();
        kill_switch_control();
        std::cout << "native lifecycle tests passed\n";
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "native lifecycle test failure: " << error.what() << '\n';
        return 1;
    }
}
