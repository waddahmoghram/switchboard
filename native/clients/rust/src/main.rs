use std::env;
use std::error::Error;
use std::fs::File;
use std::io::Read;

use switchboard_native::{verify_backup, KillSwitch, Origin, Outcome, Resolution, Runtime};

const USAGE: &str = "Usage: switchboard-rust CONFIG.json COMMAND [ARGUMENTS]
       switchboard-rust verify-backup DIRECTORY
Commands: catalog | tick | call JSON | outcome JSON | rpc JSON | approval ID | approve ID | deny ID |
          actions [FILTER_JSON] | reconcile ID applied|not_applied NOTE |
          kill-switch status|on|off | backup DIRECTORY | purge dry-run|apply";

fn main() {
    if let Err(error) = run() {
        eprintln!("ERROR: {error}");
        std::process::exit(1);
    }
}

fn read_configuration(path: &str) -> Result<String, Box<dyn Error>> {
    let mut bytes = Vec::new();
    File::open(path)?
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 1024 * 1024 {
        return Err("native Rust configuration exceeds its byte limit".into());
    }
    Ok(String::from_utf8(bytes)?)
}

/// Identifiers are validated lowercase hexadecimal, so they need no JSON escaping.
fn identifier_json(identifier: Option<&str>) -> String {
    identifier.map_or_else(|| "null".to_owned(), |text| format!("\"{text}\""))
}

/// One JSON line: the typed fields, then the complete report the library returned.
fn outcome_json(outcome: &Outcome) -> String {
    format!(
        "{{\"outcome\":{{\"status\":\"{}\",\"effect\":\"{}\",\"approval_id\":{},\"action_id\":{}}},\"report\":{}}}",
        outcome.status,
        outcome.effect,
        identifier_json(outcome.approval_id.as_deref()),
        identifier_json(outcome.action_id.as_deref()),
        outcome.report
    )
}

fn resolution(word: &str) -> Result<Resolution, Box<dyn Error>> {
    match word {
        "applied" => Ok(Resolution::Applied),
        "not_applied" => Ok(Resolution::NotApplied),
        _ => Err("reconcile resolution must be applied or not_applied".into()),
    }
}

fn kill_switch(word: &str) -> Result<KillSwitch, Box<dyn Error>> {
    match word {
        "status" => Ok(KillSwitch::Report),
        "off" => Ok(KillSwitch::Off),
        "on" => Ok(KillSwitch::On),
        _ => Err("kill-switch takes status, on or off".into()),
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let arguments: Vec<String> = env::args().collect();
    if arguments.len() == 3 && arguments[1] == "verify-backup" {
        // Verification opens the backup directory read-only and needs no configuration or runtime.
        println!("{}", verify_backup(&arguments[2])?);
        return Ok(());
    }
    if !(3..=6).contains(&arguments.len()) {
        return Err(USAGE.into());
    }
    let runtime = Runtime::new(&read_configuration(&arguments[1])?)?;
    let words: Vec<&str> = arguments[3..].iter().map(String::as_str).collect();
    let report = match (arguments[2].as_str(), words.as_slice()) {
        ("catalog", []) => runtime.catalog()?,
        ("tick", []) => runtime.tick()?,
        ("call", [request]) => runtime.call(request, Origin::Assistant)?,
        ("outcome", [request]) => outcome_json(&runtime.call_outcome(request, Origin::Assistant)?),
        ("rpc", [request]) => runtime.rpc(request)?,
        ("approval", [id]) => runtime.approval(id)?,
        ("approve", [id]) => runtime.approve(id)?,
        ("deny", [id]) => runtime.deny(id)?,
        ("actions", []) => runtime.actions("")?,
        ("actions", [filter]) => runtime.actions(filter)?,
        ("reconcile", [id, word, note]) => runtime.reconcile(id, resolution(word)?, note)?,
        ("kill-switch", [word]) => runtime.kill_switch(kill_switch(word)?)?,
        ("backup", [path]) => runtime.backup(path)?,
        ("purge", ["dry-run"]) => runtime.purge(true)?,
        ("purge", ["apply"]) => runtime.purge(false)?,
        _ => return Err("unsupported native Rust command or argument count".into()),
    };
    println!("{report}");
    Ok(())
}
