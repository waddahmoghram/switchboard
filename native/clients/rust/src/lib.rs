//! Safe ownership bindings for the C++ engine; this is not a second connector implementation.

use std::error::Error as StdError;
use std::fmt;
use std::fmt::Write as _;
use std::marker::PhantomData;
use std::os::raw::c_int;
use std::path::Path;
use std::ptr::NonNull;
use std::rc::Rc;

const ABI_VERSION: u32 = 1;
const MINIMUM_ABI_REVISION: u32 = 2;
const MAX_INPUT_BYTES: usize = 1024 * 1024;
const MAX_OUTPUT_BYTES: usize = 8 * MAX_INPUT_BYTES;
const IDENTIFIER_BYTES: usize = 32;

mod ffi {
    use std::os::raw::c_int;

    #[repr(C)]
    pub struct Runtime {
        _private: [u8; 0],
    }

    #[repr(C)]
    pub struct Buffer {
        pub data: *mut u8,
        pub length: usize,
    }

    /// Mirrors `sb_outcome`. C `char` has the size and alignment of `u8`, so the layouts match.
    #[repr(C)]
    pub struct Outcome {
        pub status: i32,
        pub effect: i32,
        pub approval_id: [u8; 33],
        pub action_id: [u8; 33],
    }

    #[link(name = "switchboard_native")]
    extern "C" {
        pub fn sb_abi_version() -> u32;
        pub fn sb_abi_revision() -> u32;
        pub fn sb_runtime_create(
            configuration: *const u8,
            length: usize,
            output: *mut *mut Runtime,
            error: *mut Buffer,
        ) -> c_int;
        pub fn sb_runtime_destroy(runtime: *mut Runtime);
        pub fn sb_runtime_catalog(runtime: *mut Runtime, output: *mut Buffer) -> c_int;
        pub fn sb_runtime_call(
            runtime: *mut Runtime,
            request: *const u8,
            length: usize,
            origin: c_int,
            output: *mut Buffer,
        ) -> c_int;
        pub fn sb_runtime_call_outcome(
            runtime: *mut Runtime,
            request: *const u8,
            length: usize,
            origin: c_int,
            outcome: *mut Outcome,
            report: *mut Buffer,
        ) -> c_int;
        pub fn sb_runtime_approval(
            runtime: *mut Runtime,
            id: *const u8,
            length: usize,
            output: *mut Buffer,
        ) -> c_int;
        pub fn sb_runtime_approve(
            runtime: *mut Runtime,
            id: *const u8,
            length: usize,
            output: *mut Buffer,
        ) -> c_int;
        pub fn sb_runtime_deny(
            runtime: *mut Runtime,
            id: *const u8,
            length: usize,
            output: *mut Buffer,
        ) -> c_int;
        pub fn sb_runtime_tick(runtime: *mut Runtime, output: *mut Buffer) -> c_int;
        pub fn sb_runtime_rpc(
            runtime: *mut Runtime,
            request: *const u8,
            length: usize,
            output: *mut Buffer,
        ) -> c_int;
        pub fn sb_runtime_actions(
            runtime: *mut Runtime,
            filter: *const u8,
            length: usize,
            output: *mut Buffer,
        ) -> c_int;
        pub fn sb_runtime_reconcile(
            runtime: *mut Runtime,
            request: *const u8,
            length: usize,
            output: *mut Buffer,
        ) -> c_int;
        pub fn sb_runtime_kill_switch(
            runtime: *mut Runtime,
            mode: i32,
            output: *mut Buffer,
        ) -> c_int;
        pub fn sb_runtime_backup(
            runtime: *mut Runtime,
            path: *const u8,
            length: usize,
            output: *mut Buffer,
        ) -> c_int;
        pub fn sb_runtime_purge(runtime: *mut Runtime, dry_run: i32, output: *mut Buffer) -> c_int;
        pub fn sb_verify_backup(path: *const u8, length: usize, output: *mut Buffer) -> c_int;
        pub fn sb_runtime_cancel(runtime: *mut Runtime);
        pub fn sb_buffer_free(output: *mut Buffer);
    }
}

#[derive(Debug)]
pub enum Error {
    InputTooLarge,
    AbiMismatch,
    InvalidNativeBuffer,
    InvalidNativeOutcome,
    InvalidUtf8,
    InvalidPath,
    Native { status: c_int, report: String },
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InputTooLarge => formatter.write_str("native input exceeds its byte limit"),
            Self::AbiMismatch => formatter.write_str("native library interface version mismatch"),
            Self::InvalidNativeBuffer => {
                formatter.write_str("native library returned an invalid buffer")
            }
            Self::InvalidNativeOutcome => {
                formatter.write_str("native library returned an invalid typed outcome")
            }
            Self::InvalidUtf8 => formatter.write_str("native library returned invalid UTF-8"),
            Self::InvalidPath => formatter.write_str("native paths must be valid UTF-8"),
            Self::Native { status, report } => {
                write!(formatter, "native interface error {status}: {report}")
            }
        }
    }
}

impl StdError for Error {}

struct Buffer(ffi::Buffer);

impl Buffer {
    fn empty() -> Self {
        Self(ffi::Buffer {
            data: std::ptr::null_mut(),
            length: 0,
        })
    }

    fn text(&self) -> Result<String, Error> {
        if self.0.length == 0 {
            return Ok(String::new());
        }
        if self.0.data.is_null() || self.0.length > MAX_OUTPUT_BYTES {
            return Err(Error::InvalidNativeBuffer);
        }
        // SAFETY: matching-version C buffers remain allocated until this owner's Drop.
        let bytes = unsafe { std::slice::from_raw_parts(self.0.data, self.0.length) };
        String::from_utf8(bytes.to_vec()).map_err(|_| Error::InvalidUtf8)
    }

    fn finish(self, status: c_int) -> Result<String, Error> {
        let report = self.text()?;
        if status != 0 {
            return Err(Error::Native { status, report });
        }
        Ok(report)
    }
}

impl Drop for Buffer {
    fn drop(&mut self) {
        // SAFETY: only the native allocator created this buffer; freeing resets its fields.
        unsafe { ffi::sb_buffer_free(&mut self.0) };
    }
}

#[derive(Clone, Copy, Debug)]
pub enum Origin {
    Assistant,
    Rule,
    Human,
}

impl Origin {
    fn value(self) -> c_int {
        match self {
            Self::Assistant => 0,
            Self::Rule => 1,
            Self::Human => 2,
        }
    }
}

/// The JSON outcome status of a call. `Ok` means a handler returned a report, not that an effect happened.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Status {
    Ok,
    Pending,
    Denied,
    Invalid,
    Error,
}

impl Status {
    fn from_raw(value: i32) -> Result<Self, Error> {
        match value {
            0 => Ok(Self::Ok),
            1 => Ok(Self::Pending),
            2 => Ok(Self::Denied),
            3 => Ok(Self::Invalid),
            4 => Ok(Self::Error),
            _ => Err(Error::InvalidNativeOutcome),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Pending => "pending",
            Self::Denied => "denied",
            Self::Invalid => "invalid",
            Self::Error => "error",
        }
    }
}

impl fmt::Display for Status {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// What is known about an external effect, derived from the action journal as in `contracts/README.md`.
/// `Confirmed` means a software readback matched the requested state; it never proves a physical effect.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Effect {
    /// No handler executed a write: a read, a pending approval, or a refusal before execution.
    None,
    /// A write handler returned a report without saying whether it read the state back.
    Returned,
    Confirmed,
    Unconfirmed,
    /// The effect might or might not have happened. Inspect and reconcile; never blindly repeat it.
    Unknown,
    /// The handler asserted the effect did not happen.
    Failed,
}

impl Effect {
    fn from_raw(value: i32) -> Result<Self, Error> {
        match value {
            0 => Ok(Self::None),
            1 => Ok(Self::Returned),
            2 => Ok(Self::Confirmed),
            3 => Ok(Self::Unconfirmed),
            4 => Ok(Self::Unknown),
            5 => Ok(Self::Failed),
            _ => Err(Error::InvalidNativeOutcome),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Returned => "returned",
            Self::Confirmed => "confirmed",
            Self::Unconfirmed => "unconfirmed",
            Self::Unknown => "unknown",
            Self::Failed => "failed",
        }
    }
}

impl fmt::Display for Effect {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// A typed call outcome plus the complete JSON report it was derived from.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    pub status: Status,
    pub effect: Effect,
    pub approval_id: Option<String>,
    pub action_id: Option<String>,
    pub report: String,
}

/// An operator's decision about an uncertain journal entry. It records a human finding; it never repeats,
/// reverses or verifies the effect.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Resolution {
    Applied,
    NotApplied,
}

impl Resolution {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Applied => "applied",
            Self::NotApplied => "not_applied",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KillSwitch {
    Report,
    Off,
    On,
}

impl KillSwitch {
    fn value(self) -> i32 {
        match self {
            Self::Report => -1,
            Self::Off => 0,
            Self::On => 1,
        }
    }
}

/// Owns one native runtime. No Send/Sync implementation is promised by this binding; use
/// [`Runtime::cancel_handle`] to cancel from another thread.
pub struct Runtime {
    handle: NonNull<ffi::Runtime>,
    _thread_local: PhantomData<Rc<()>>,
}

/// Cancels web transfers of the call currently running on its runtime, from any thread. Cancelling when no
/// call runs does nothing and does not affect later calls. A cancelled write's effect is unknown.
#[derive(Clone, Copy, Debug)]
pub struct CancelHandle<'runtime> {
    handle: NonNull<ffi::Runtime>,
    _runtime: PhantomData<&'runtime Runtime>,
}

// SAFETY: the only operation is sb_runtime_cancel, which the C interface documents as thread-safe and
// lock-free: it reads the handle and atomically signals the call in progress, never taking the runtime lock
// or touching other runtime state. The borrow of the runtime prevents its destruction for 'runtime, so the
// pointer stays valid on whichever thread holds the handle.
unsafe impl Send for CancelHandle<'_> {}
// SAFETY: as for Send; a shared reference allows nothing beyond the same thread-safe cancellation.
unsafe impl Sync for CancelHandle<'_> {}

impl CancelHandle<'_> {
    pub fn cancel(&self) {
        // SAFETY: the borrowed runtime is alive, and the C function is thread-safe and lock-free.
        unsafe { ffi::sb_runtime_cancel(self.handle.as_ptr()) };
    }
}

fn input_size(text: &str) -> Result<(), Error> {
    if text.len() > MAX_INPUT_BYTES {
        return Err(Error::InputTooLarge);
    }
    Ok(())
}

fn check_abi() -> Result<(), Error> {
    // SAFETY: this version query has no pointers or ownership transfer.
    if unsafe { ffi::sb_abi_version() } != ABI_VERSION {
        return Err(Error::AbiMismatch);
    }
    // SAFETY: as above; revision 2 libraries export this query.
    if unsafe { ffi::sb_abi_revision() } < MINIMUM_ABI_REVISION {
        return Err(Error::AbiMismatch);
    }
    Ok(())
}

fn path_text(path: &Path) -> Result<&str, Error> {
    let text = path.to_str().ok_or(Error::InvalidPath)?;
    input_size(text)?;
    Ok(text)
}

/// A NUL-terminated identifier field: empty means absent; anything else must be 32 lowercase hex digits.
fn identifier(field: &[u8; 33]) -> Result<Option<String>, Error> {
    let length = field
        .iter()
        .position(|byte| *byte == 0)
        .ok_or(Error::InvalidNativeOutcome)?;
    if length == 0 {
        return Ok(None);
    }
    let text = &field[..length];
    if length != IDENTIFIER_BYTES
        || !text
            .iter()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    {
        return Err(Error::InvalidNativeOutcome);
    }
    String::from_utf8(text.to_vec())
        .map(Some)
        .map_err(|_| Error::InvalidNativeOutcome)
}

fn json_string(text: &str) -> String {
    let mut result = String::with_capacity(text.len() + 2);
    result.push('"');
    for character in text.chars() {
        match character {
            '"' => result.push_str("\\\""),
            '\\' => result.push_str("\\\\"),
            control if u32::from(control) < 0x20 => {
                // Writing to a String cannot fail.
                let _ = write!(result, "\\u{:04x}", u32::from(control));
            }
            other => result.push(other),
        }
    }
    result.push('"');
    result
}

/// Verifies a backup directory read-only (manifest digest, database integrity, schema version and audit head)
/// without a runtime. A missing or altered backup is an `Ok` report with `"verified":false`, not an `Err`. A
/// passing report does not prove that providers or devices still match the backup.
pub fn verify_backup(source: impl AsRef<Path>) -> Result<String, Error> {
    check_abi()?;
    let path = path_text(source.as_ref())?;
    let mut output = Buffer::empty();
    // SAFETY: the borrowed path is not retained, and the empty output is owned by this frame.
    let status = unsafe { ffi::sb_verify_backup(path.as_ptr(), path.len(), &mut output.0) };
    output.finish(status)
}

impl Runtime {
    pub fn new(configuration_json: &str) -> Result<Self, Error> {
        input_size(configuration_json)?;
        check_abi()?;
        let mut handle = std::ptr::null_mut();
        let mut error = Buffer::empty();
        // SAFETY: input slices and zero-initialized outputs live for the complete call.
        let status = unsafe {
            ffi::sb_runtime_create(
                configuration_json.as_ptr(),
                configuration_json.len(),
                &mut handle,
                &mut error.0,
            )
        };
        if status != 0 {
            return Err(Error::Native {
                status,
                report: error.text()?,
            });
        }
        let handle = NonNull::new(handle).ok_or(Error::InvalidNativeBuffer)?;
        Ok(Self {
            handle,
            _thread_local: PhantomData,
        })
    }

    fn invoke(
        &self,
        function: impl FnOnce(*mut ffi::Runtime, *mut ffi::Buffer) -> c_int,
    ) -> Result<String, Error> {
        let mut output = Buffer::empty();
        let status = function(self.handle.as_ptr(), &mut output.0);
        output.finish(status)
    }

    pub fn catalog(&self) -> Result<String, Error> {
        self.invoke(|runtime, output| {
            // SAFETY: self owns the live handle and invoke owns the empty output.
            unsafe { ffi::sb_runtime_catalog(runtime, output) }
        })
    }

    /// Returns the JSON report; Ok is not proof of approval, delivery or a physical effect.
    pub fn call(&self, request_json: &str, origin: Origin) -> Result<String, Error> {
        input_size(request_json)?;
        self.invoke(|runtime, output| {
            // SAFETY: the borrowed input remains valid and is not retained by the native call.
            unsafe {
                ffi::sb_runtime_call(
                    runtime,
                    request_json.as_ptr(),
                    request_json.len(),
                    origin.value(),
                    output,
                )
            }
        })
    }

    /// Like [`Runtime::call`], with the status, effect and identifiers decoded by the native library.
    /// An `Err` after the call started can hide an effect; inspect [`Runtime::actions`] before retrying.
    pub fn call_outcome(&self, request_json: &str, origin: Origin) -> Result<Outcome, Error> {
        input_size(request_json)?;
        let mut raw = ffi::Outcome {
            status: 0,
            effect: 0,
            approval_id: [0; 33],
            action_id: [0; 33],
        };
        let mut report = Buffer::empty();
        // SAFETY: the handle is live, the borrowed request is not retained, and both outputs belong to
        // this frame for the complete call.
        let status = unsafe {
            ffi::sb_runtime_call_outcome(
                self.handle.as_ptr(),
                request_json.as_ptr(),
                request_json.len(),
                origin.value(),
                &mut raw,
                &mut report.0,
            )
        };
        let report = report.finish(status)?;
        Ok(Outcome {
            status: Status::from_raw(raw.status)?,
            effect: Effect::from_raw(raw.effect)?,
            approval_id: identifier(&raw.approval_id)?,
            action_id: identifier(&raw.action_id)?,
            report,
        })
    }

    /// Operator review of one approval, including the exact stored arguments that approving executes. A
    /// malformed or unknown identifier is an `Ok` report whose status is `invalid`, as with `approve`.
    pub fn approval(&self, id: &str) -> Result<String, Error> {
        input_size(id)?;
        self.invoke(|runtime, output| {
            // SAFETY: handle/output/input lifetimes are confined to invoke.
            unsafe { ffi::sb_runtime_approval(runtime, id.as_ptr(), id.len(), output) }
        })
    }

    pub fn approve(&self, id: &str) -> Result<String, Error> {
        input_size(id)?;
        self.invoke(|runtime, output| {
            // SAFETY: handle/output/input lifetimes are confined to invoke.
            unsafe { ffi::sb_runtime_approve(runtime, id.as_ptr(), id.len(), output) }
        })
    }

    pub fn deny(&self, id: &str) -> Result<String, Error> {
        input_size(id)?;
        self.invoke(|runtime, output| {
            // SAFETY: handle/output/input lifetimes are confined to invoke.
            unsafe { ffi::sb_runtime_deny(runtime, id.as_ptr(), id.len(), output) }
        })
    }

    pub fn tick(&self) -> Result<String, Error> {
        self.invoke(|runtime, output| {
            // SAFETY: self owns the live handle and invoke owns the empty output.
            unsafe { ffi::sb_runtime_tick(runtime, output) }
        })
    }

    pub fn rpc(&self, request_json: &str) -> Result<String, Error> {
        input_size(request_json)?;
        self.invoke(|runtime, output| {
            // SAFETY: request bytes remain valid for the call; native code does not retain them.
            unsafe {
                ffi::sb_runtime_rpc(runtime, request_json.as_ptr(), request_json.len(), output)
            }
        })
    }

    /// Operator view of the action journal. The filter is JSON, `{"state"?, "limit"?}` or `{"id"}`; an
    /// empty string means no filter.
    pub fn actions(&self, filter_json: &str) -> Result<String, Error> {
        input_size(filter_json)?;
        self.invoke(|runtime, output| {
            // SAFETY: filter bytes remain valid for the call; native code does not retain them.
            unsafe {
                ffi::sb_runtime_actions(runtime, filter_json.as_ptr(), filter_json.len(), output)
            }
        })
    }

    /// Operator reconciliation of an uncertain journal entry, with a note of 1 to 500 printable characters.
    pub fn reconcile(
        &self,
        action_id: &str,
        resolution: Resolution,
        note: &str,
    ) -> Result<String, Error> {
        let request = format!(
            "{{\"id\":{},\"resolution\":\"{}\",\"note\":{}}}",
            json_string(action_id),
            resolution.as_str(),
            json_string(note)
        );
        input_size(&request)?;
        self.invoke(|runtime, output| {
            // SAFETY: the request lives until invoke returns; native code does not retain it.
            unsafe { ffi::sb_runtime_reconcile(runtime, request.as_ptr(), request.len(), output) }
        })
    }

    /// Reports or sets the kill switch, which blocks non-read calls; it does not stop reads or calls
    /// already running.
    pub fn kill_switch(&self, mode: KillSwitch) -> Result<String, Error> {
        self.invoke(|runtime, output| {
            // SAFETY: self owns the live handle and invoke owns the empty output.
            unsafe { ffi::sb_runtime_kill_switch(runtime, mode.value(), output) }
        })
    }

    /// Writes a consistent online copy of the native state and its manifest into a new or empty private
    /// directory, created if absent. A refused destination is an `Ok` report whose status is `error`.
    pub fn backup(&self, destination: impl AsRef<Path>) -> Result<String, Error> {
        let path = path_text(destination.as_ref())?;
        self.invoke(|runtime, output| {
            // SAFETY: path bytes remain valid for the call; native code does not retain them.
            unsafe { ffi::sb_runtime_backup(runtime, path.as_ptr(), path.len(), output) }
        })
    }

    /// Applies core retention. Open journal entries (in flight or uncertain) are never purged.
    pub fn purge(&self, dry_run: bool) -> Result<String, Error> {
        self.invoke(|runtime, output| {
            // SAFETY: self owns the live handle and invoke owns the empty output.
            unsafe { ffi::sb_runtime_purge(runtime, i32::from(dry_run), output) }
        })
    }

    /// A handle that may cancel this runtime's call in progress from another thread, for example one started
    /// with `std::thread::scope`. The borrow keeps the runtime alive while any handle exists.
    pub fn cancel_handle(&self) -> CancelHandle<'_> {
        CancelHandle {
            handle: self.handle,
            _runtime: PhantomData,
        }
    }
}

impl Drop for Runtime {
    fn drop(&mut self) {
        // SAFETY: unique ownership prevents another Drop or any later safe use of this handle.
        unsafe { ffi::sb_runtime_destroy(self.handle.as_ptr()) };
    }
}

#[cfg(test)]
mod tests {
    use super::{
        identifier, input_size, json_string, CancelHandle, Effect, Error, Status, MAX_INPUT_BYTES,
    };

    fn field(text: &[u8]) -> [u8; 33] {
        let mut result = [0; 33];
        result[..text.len()].copy_from_slice(text);
        result
    }

    #[test]
    fn byte_limit_is_measured_before_entering_native_code() {
        assert!(input_size(&"x".repeat(MAX_INPUT_BYTES)).is_ok());
        assert!(matches!(
            input_size(&"x".repeat(MAX_INPUT_BYTES + 1)),
            Err(Error::InputTooLarge)
        ));
    }

    #[test]
    fn limit_counts_utf8_bytes_not_visible_characters() {
        assert!(matches!(
            input_size(&"\u{e9}".repeat(MAX_INPUT_BYTES)),
            Err(Error::InputTooLarge)
        ));
    }

    #[test]
    fn typed_values_match_the_c_header_numbering() {
        let statuses = [
            Status::Ok,
            Status::Pending,
            Status::Denied,
            Status::Invalid,
            Status::Error,
        ];
        for (raw, status) in statuses.iter().enumerate() {
            assert_eq!(Status::from_raw(raw as i32).unwrap(), *status);
        }
        let effects = [
            Effect::None,
            Effect::Returned,
            Effect::Confirmed,
            Effect::Unconfirmed,
            Effect::Unknown,
            Effect::Failed,
        ];
        for (raw, effect) in effects.iter().enumerate() {
            assert_eq!(Effect::from_raw(raw as i32).unwrap(), *effect);
        }
        assert!(matches!(
            Status::from_raw(5),
            Err(Error::InvalidNativeOutcome)
        ));
        assert!(matches!(
            Effect::from_raw(-1),
            Err(Error::InvalidNativeOutcome)
        ));
    }

    #[test]
    fn identifiers_are_absent_or_32_lowercase_hex_digits() {
        assert_eq!(identifier(&[0; 33]).unwrap(), None);
        let valid = "0123456789abcdef0123456789abcdef";
        assert_eq!(
            identifier(&field(valid.as_bytes())).unwrap().as_deref(),
            Some(valid)
        );
        for invalid in [
            "0123456789ABCDEF0123456789ABCDEF".as_bytes(),
            "0123456789abcdef".as_bytes(),
            "0123456789abcdef0123456789abcdeg".as_bytes(),
        ] {
            assert!(matches!(
                identifier(&field(invalid)),
                Err(Error::InvalidNativeOutcome)
            ));
        }
        assert!(matches!(
            identifier(&[b'a'; 33]),
            Err(Error::InvalidNativeOutcome)
        ));
    }

    #[test]
    fn reconcile_text_is_escaped_as_json() {
        assert_eq!(json_string("plain"), "\"plain\"");
        assert_eq!(
            json_string("quote \" slash \\ line\n tab\t bell\u{7}"),
            "\"quote \\\" slash \\\\ line\\u000a tab\\u0009 bell\\u0007\""
        );
        assert_eq!(json_string("\u{e9}"), "\"\u{e9}\"");
    }

    #[test]
    fn cancel_handles_cross_threads() {
        fn assert_send_sync<T: Send + Sync>() {}
        assert_send_sync::<CancelHandle<'static>>();
    }
}
