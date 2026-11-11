use std::env;
use std::error::Error;
use std::fs::File;
use std::io::Read;

use switchboard_native::{Origin, Runtime};

fn main() {
    if let Err(error) = run() {
        eprintln!("ERROR: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let arguments: Vec<String> = env::args().collect();
    if !(3..=4).contains(&arguments.len()) {
        return Err(
            "Usage: switchboard-rust CONFIG.json catalog|call|rpc|approve|deny|tick [JSON_OR_ID]"
                .into(),
        );
    }
    let mut bytes = Vec::new();
    File::open(&arguments[1])?
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 1024 * 1024 {
        return Err("native Rust configuration exceeds its byte limit".into());
    }
    let configuration = String::from_utf8(bytes)?;
    let runtime = Runtime::new(&configuration)?;
    let argument = arguments.get(3).map(String::as_str);
    let report = match (arguments[2].as_str(), argument) {
        ("catalog", None) => runtime.catalog()?,
        ("tick", None) => runtime.tick()?,
        ("call", Some(request)) => runtime.call(request, Origin::Assistant)?,
        ("rpc", Some(request)) => runtime.rpc(request)?,
        ("approve", Some(id)) => runtime.approve(id)?,
        ("deny", Some(id)) => runtime.deny(id)?,
        _ => return Err("unsupported native Rust command or argument count".into()),
    };
    println!("{report}");
    Ok(())
}
