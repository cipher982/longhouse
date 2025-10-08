//! Tiny toast / notification helper.
//! Creates a `#toast-container` container once per page and appends toast divs that
//! fade-out after a few seconds.

use wasm_bindgen::{closure::Closure, JsCast};
use web_sys::{Document, Element, HtmlElement};

#[derive(Debug, Clone, Copy)]
pub enum ToastKind {
    Success,
    Error,
    Info,
}

pub fn success(msg: &str) {
    show(msg, ToastKind::Success);
}

pub fn error(msg: &str) {
    show(msg, ToastKind::Error);
}

pub fn info(msg: &str) {
    show(msg, ToastKind::Info);
}

pub fn show(message: &str, kind: ToastKind) {
    const CONTAINER_ID: &str = "toast-container";

    let window = match web_sys::window() {
        Some(w) => w,
        None => return,
    };
    let document = match window.document() {
        Some(d) => d,
        None => return,
    };

    let root = ensure_root(&document, CONTAINER_ID);

    let toast = document.create_element("div").unwrap();
    toast.set_class_name("toast");
    match kind {
        ToastKind::Success => toast.class_list().add_1("toast-success").unwrap(),
        ToastKind::Error => toast.class_list().add_1("toast-error").unwrap(),
        ToastKind::Info => toast.class_list().add_1("toast-info").unwrap(),
    };
    toast.set_text_content(Some(message));

    // Prepend so newest appears on top.
    let _ = root.prepend_with_node_1(&toast);

    // Auto-remove after 4s.
    let toast_clone: HtmlElement = toast.unchecked_into();
    let cb = Closure::once_into_js(move || {
        let _ = toast_clone
            .parent_node()
            .map(|p| p.remove_child(&toast_clone));
    });
    let _ = window
        .set_timeout_with_callback_and_timeout_and_arguments_0(cb.as_ref().unchecked_ref(), 4000);

    ensure_spinner_styles(&document);
}

fn ensure_root(document: &Document, id: &str) -> Element {
    if let Some(el) = document.get_element_by_id(id) {
        el
    } else {
        let root = document.create_element("div").unwrap();
        root.set_id(id);
        root.set_class_name(id); // class name same as id for CSS hook
        document.body().unwrap().append_child(&root).unwrap();
        root
    }
}

fn ensure_spinner_styles(document: &Document) {
    if document.get_element_by_id("spinner-styles").is_some() {
        return;
    }

    let css = "
/* spinner for buttons */
.spinner{display:inline-block;width:14px;height:14px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin 1s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
";

    let style = document.create_element("style").unwrap();
    style.set_id("spinner-styles");
    style.set_text_content(Some(css));
    if let Some(head) = document.query_selector("head").unwrap() {
        head.append_child(&style).unwrap();
    } else {
        document.body().unwrap().append_child(&style).unwrap();
    }
}
