use std::ffi::OsStr;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use switchboard_native::{
    verify_backup, Effect, Error, KillSwitch, Origin, Resolution, Runtime, Status,
};

const STATUS: &str = r#"{"tool":"switchboard_status","arguments":{}}"#;

struct Directory(PathBuf);

impl Directory {
    fn new() -> Self {
        let token = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "switchboard-rust-outcome-test-{}-{token}",
            std::process::id()
        ));
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        Self(path)
    }
}

impl Drop for Directory {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_dir_all(&self.0) {
            eprintln!("ERROR: native Rust test cleanup failed: {error}");
        }
    }
}

fn quoted(path: &Path) -> String {
    path.to_str()
        .unwrap()
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
        .replace('\t', "\\t")
}

fn configuration(directory: &Directory, connectors: &str, policy: &str) -> String {
    format!(
        r#"{{"switchboard":{{"connectors":[{connectors}],"data_dir":"{}"}},"policy":{{"tools":{{{policy}}}}},"mcp":{{"expose":["*"]}}}}"#,
        quoted(&directory.0)
    )
}

fn is_identifier(text: &str) -> bool {
    text.len() == 32
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

fn kill_switch_on(runtime: &Runtime) -> bool {
    runtime
        .call(STATUS, Origin::Human)
        .unwrap()
        .contains(r#""kill_switch":true"#)
}

#[test]
fn read_calls_have_no_effect_and_no_identifiers() {
    let directory = Directory::new();
    let runtime = Runtime::new(&configuration(&directory, "", "")).unwrap();
    let outcome = runtime.call_outcome(STATUS, Origin::Assistant).unwrap();
    assert_eq!(outcome.status, Status::Ok);
    assert_eq!(outcome.effect, Effect::None);
    assert_eq!(outcome.approval_id, None);
    assert_eq!(outcome.action_id, None);
    assert!(outcome.report.contains(r#""tool":"switchboard_status""#));
}

#[test]
fn pending_denied_and_invalid_outcomes_are_typed() {
    let directory = Directory::new();
    let policy = r#""events_recent":{"decision":"ask"},"jobs_status":{"decision":"deny"}"#;
    let runtime = Runtime::new(&configuration(&directory, "", policy)).unwrap();

    let pending = runtime
        .call_outcome(
            r#"{"tool":"events_recent","arguments":{}}"#,
            Origin::Assistant,
        )
        .unwrap();
    assert_eq!(pending.status, Status::Pending);
    assert_eq!(pending.effect, Effect::None);
    assert_eq!(pending.action_id, None);
    let approval = pending.approval_id.clone().unwrap();
    assert!(is_identifier(&approval) && pending.report.contains(&approval));
    // Reviewing shows the exact stored arguments and decides nothing; unknown identifiers are invalid reports.
    let review = runtime.approval(&approval).unwrap();
    assert!(review.contains(r#""args":{}"#) && review.contains(r#""status":"pending""#));
    for unknown in ["x".to_owned(), "0".repeat(32)] {
        assert!(runtime
            .approval(&unknown)
            .unwrap()
            .contains(r#""status":"invalid""#));
    }
    assert!(runtime.deny(&approval).unwrap().contains("denied"));

    let denied = runtime
        .call_outcome(
            r#"{"tool":"jobs_status","arguments":{}}"#,
            Origin::Assistant,
        )
        .unwrap();
    assert_eq!(
        (denied.status, denied.effect),
        (Status::Denied, Effect::None)
    );

    let invalid = runtime
        .call_outcome(
            r#"{"tool":"not_implemented","arguments":{}}"#,
            Origin::Assistant,
        )
        .unwrap();
    assert_eq!(
        (invalid.status, invalid.effect),
        (Status::Invalid, Effect::None)
    );
}

#[test]
fn local_writes_name_their_journal_entries() {
    let directory = Directory::new();
    let runtime = Runtime::new(&configuration(&directory, r#""tasks""#, "")).unwrap();
    let added = runtime
        .call_outcome(
            r#"{"tool":"tasks_add","arguments":{"title":"typed outcome"}}"#,
            Origin::Assistant,
        )
        .unwrap();
    assert_eq!(added.status, Status::Ok);
    assert!(matches!(
        added.effect,
        Effect::Returned | Effect::Confirmed | Effect::Unconfirmed
    ));
    let action = added.action_id.clone().unwrap();
    assert!(is_identifier(&action) && added.report.contains(&action));

    // The handler refuses an update without fields before writing, so the attempt finishes as failed.
    let refused = runtime
        .call_outcome(
            r#"{"tool":"tasks_update","arguments":{"id":1,"revision":1}}"#,
            Origin::Assistant,
        )
        .unwrap();
    assert_eq!(
        (refused.status, refused.effect),
        (Status::Error, Effect::Failed)
    );
    let failed = refused.action_id.unwrap();

    // The handler rejects a zero byte in the notes after the attempt was recorded: the report is invalid, and
    // the journal entry, checked before the status, makes the effect failed.
    let rejected = runtime
        .call_outcome(
            r#"{"tool":"tasks_add","arguments":{"title":"x","notes":"a\u0000b"}}"#,
            Origin::Assistant,
        )
        .unwrap();
    assert_eq!(
        (rejected.status, rejected.effect),
        (Status::Invalid, Effect::Failed)
    );
    assert!(rejected.action_id.as_deref().map_or(false, is_identifier));

    let journal = runtime.actions("").unwrap();
    assert!(journal.contains(&action) && journal.contains(&failed));
    assert!(runtime
        .actions(r#"{"state":"failed"}"#)
        .unwrap()
        .contains(&failed));

    // Only uncertain attempts can be reconciled; a succeeded attempt stays as it was.
    match runtime.reconcile(&action, Resolution::Applied, "checked by the authored test") {
        Ok(report) => assert!(!report.contains("reconciled_applied")),
        Err(Error::Native { .. }) => {}
        Err(other) => panic!("unexpected reconcile failure: {other}"),
    }
}

#[test]
fn malformed_requests_are_errors_not_outcomes() {
    let directory = Directory::new();
    let runtime = Runtime::new(&configuration(&directory, "", "")).unwrap();
    assert!(matches!(
        runtime.call_outcome("{", Origin::Assistant),
        Err(Error::Native { status: 1, .. })
    ));
    assert!(matches!(
        runtime.actions("[]"),
        Err(Error::Native { status: 1, .. })
    ));
    assert!(matches!(
        runtime.call_outcome(&"x".repeat(1024 * 1024 + 1), Origin::Assistant),
        Err(Error::InputTooLarge)
    ));
}

#[test]
fn operator_controls_report_through_the_wrapper() {
    let directory = Directory::new();
    let runtime = Runtime::new(&configuration(&directory, "", "")).unwrap();
    runtime.kill_switch(KillSwitch::On).unwrap();
    assert!(kill_switch_on(&runtime));
    runtime.kill_switch(KillSwitch::Report).unwrap();
    runtime.kill_switch(KillSwitch::Off).unwrap();
    assert!(!kill_switch_on(&runtime));
    runtime.purge(true).unwrap();

    // Backups are directories: the engine creates a new private one holding the copy and its manifest.
    let backups = Directory::new();
    let copy = backups.0.join("copy");
    assert!(runtime.backup(&copy).unwrap().contains(r#""status":"ok""#));
    assert!(verify_backup(&copy).unwrap().contains(r#""verified":true"#));
    assert!(runtime
        .backup(&copy)
        .unwrap()
        .contains(r#""status":"error""#));
    // A missing backup is a report that does not claim verification, never a success.
    match verify_backup(backups.0.join("missing")) {
        Ok(report) => assert!(report.contains(r#""verified":false"#)),
        Err(Error::Native { .. }) => {}
        Err(other) => panic!("unexpected verification failure: {other}"),
    }
    let unrepresentable = Path::new(OsStr::from_bytes(b"/tmp/switchboard-\xff"));
    assert!(matches!(
        runtime.backup(unrepresentable),
        Err(Error::InvalidPath)
    ));
}

#[test]
fn cancel_handle_crosses_scoped_threads() {
    let directory = Directory::new();
    let runtime = Runtime::new(&configuration(&directory, "", "")).unwrap();
    let cancel = runtime.cancel_handle();
    std::thread::scope(|scope| {
        let worker = scope.spawn(move || {
            for _ in 0..1000 {
                cancel.cancel();
            }
        });
        // A core read has no web transfer to stop, so concurrent cancellation cannot change it.
        let outcome = runtime.call_outcome(STATUS, Origin::Assistant).unwrap();
        assert_eq!(outcome.status, Status::Ok);
        worker.join().unwrap();
    });
    // Cancelling while idle does not affect later calls.
    runtime.cancel_handle().cancel();
    assert_eq!(
        runtime
            .call_outcome(STATUS, Origin::Assistant)
            .unwrap()
            .status,
        Status::Ok
    );
}
