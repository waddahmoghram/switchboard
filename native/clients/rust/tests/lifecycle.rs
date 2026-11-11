use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::time::{SystemTime, UNIX_EPOCH};

use switchboard_native::{Origin, Runtime};

struct Directory(std::path::PathBuf);

impl Directory {
    fn new() -> Self {
        let token = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "switchboard-rust-test-{}-{token}",
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

fn configuration(directory: &Directory) -> String {
    let path = directory.0.to_str().unwrap();
    let quoted = path
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
        .replace('\t', "\\t");
    format!(
        r#"{{"switchboard":{{"connectors":[],"data_dir":"{quoted}"}},"mcp":{{"expose":["*"]}}}}"#
    )
}

#[test]
fn runtime_owns_buffers_and_needs_no_python_for_core_calls() {
    let directory = Directory::new();
    let runtime = Runtime::new(&configuration(&directory)).unwrap();
    assert!(runtime.catalog().unwrap().contains("switchboard_status"));
    let report = runtime
        .call(
            r#"{"tool":"switchboard_status","arguments":{}}"#,
            Origin::Assistant,
        )
        .unwrap();
    assert!(report.contains(r#""backend":"cpp-native""#));
    assert!(report.contains(r#""python_required":false"#));
}

#[test]
fn invalid_configuration_is_an_error_not_an_empty_runtime() {
    assert!(Runtime::new(r#"{"unexpected":true}"#).is_err());
}

#[test]
fn native_denial_is_a_report_not_physical_success() {
    let directory = Directory::new();
    let runtime = Runtime::new(&configuration(&directory)).unwrap();
    let report = runtime
        .call(
            r#"{"tool":"not_implemented","arguments":{}}"#,
            Origin::Assistant,
        )
        .unwrap();
    assert!(report.contains(r#""status":"invalid""#));
}
