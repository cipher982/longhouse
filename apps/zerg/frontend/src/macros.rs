//! Small crate-wide convenience macros.

/// Acquire a **mutable** borrow from a `RefCell` (or `Rc<RefCell>`).
/// If another mutable borrow is still active the call panics – the standard
/// panic message emitted by `RefCell::borrow_mut()` is preserved to keep the
/// macro zero-cost.
///
/// ```rust,ignore
/// use std::cell::RefCell;
/// let cell = RefCell::new(1);
/// {
///     let mut n = mut_borrow!(cell);
///     *n += 1;
/// }
/// assert_eq!(*cell.borrow(), 2);
/// ```
#[macro_export]
macro_rules! mut_borrow {
    ($cell:expr) => {
        // The explicit expect message is important – default panic is hard
        // to trace when it bubbles up from deep inside borrowed call-stacks.
        $cell.borrow_mut()
    };
}

/// Quick helper to embed CSS custom-properties (`var(--token)`) without
/// sprinkling `format!("var(--{})", token)` everywhere in the Rust/Yew code.
///
/// ```rust,ignore
/// let color = css_var!("primary");               // "var(--primary)"
/// let spacing = css_var!(spacing_md);             // "var(--spacing_md)"
/// ```
#[macro_export]
macro_rules! css_var {
    ($name:expr) => {
        format!("var(--{})", $name)
    };
    // Accept identifiers without quotes for ergonomic use
    ($name:ident) => {
        format!("var(--{})", stringify!($name))
    };
}
