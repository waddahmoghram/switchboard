//! Safe ownership bindings for the C++ engine; this is not a second connector implementation.

use std::error::Error as StdError;
use std::fmt;
use std::marker::PhantomData;
use std::os::raw::c_int;
use std::ptr::NonNull;
use std::rc::Rc;

const ABI_VERSION: u32 = 1;
const MAX_INPUT_BYTES: usize = 1024 * 1024;
const MAX_OUTPUT_BYTES: usize = 8 * MAX_INPUT_BYTES;

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

    #[link(name = "switchboard_native")]
    extern "C" {
        pub fn sb_abi_version() -> u32;
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
        pub fn sb_buffer_free(output: *mut Buffer);
    }
}

#[derive(Debug)]
pub enum Error {
    InputTooLarge,
    AbiMismatch,
    InvalidNativeBuffer,
    InvalidUtf8,
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
            Self::InvalidUtf8 => formatter.write_str("native library returned invalid UTF-8"),
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

/// Owns one native runtime. No Send/Sync implementation is promised by this binding.
pub struct Runtime {
    handle: NonNull<ffi::Runtime>,
    _thread_local: PhantomData<Rc<()>>,
}

fn input_size(text: &str) -> Result<(), Error> {
    if text.len() > MAX_INPUT_BYTES {
        return Err(Error::InputTooLarge);
    }
    Ok(())
}

impl Runtime {
    pub fn new(configuration_json: &str) -> Result<Self, Error> {
        input_size(configuration_json)?;
        // SAFETY: this version query has no pointers or ownership transfer.
        if unsafe { ffi::sb_abi_version() } != ABI_VERSION {
            return Err(Error::AbiMismatch);
        }
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
        let report = output.text()?;
        if status != 0 {
            return Err(Error::Native { status, report });
        }
        Ok(report)
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
}

impl Drop for Runtime {
    fn drop(&mut self) {
        // SAFETY: unique ownership prevents another Drop or any later safe use of this handle.
        unsafe { ffi::sb_runtime_destroy(self.handle.as_ptr()) };
    }
}

#[cfg(test)]
mod tests {
    use super::{input_size, Error, MAX_INPUT_BYTES};

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
}
