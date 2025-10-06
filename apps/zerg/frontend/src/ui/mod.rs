pub mod events;
pub mod main;
pub mod setup;

use std::cell::RefCell;
use std::rc::Rc;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;

// Animation loop function that uses message architecture
pub fn setup_animation_loop() {
    // Create a handler function for requestAnimationFrame
    let f: Rc<RefCell<Option<Closure<dyn FnMut()>>>> = Rc::new(RefCell::new(None));
    let g = f.clone();

    // Closure that will be called on each animation frame
    *g.borrow_mut() = Some(Closure::wrap(Box::new(move || {
        // Dispatch animation tick message to update state and schedule UI / side effects
        crate::state::dispatch_global_message(crate::messages::Message::AnimationTick);

        // Schedule the next frame
        let _ = web_sys::window()
            .expect("no global window")
            .request_animation_frame(f.borrow().as_ref().unwrap().as_ref().unchecked_ref());
    }) as Box<dyn FnMut()>));

    // Start the animation loop
    web_sys::window()
        .expect("no global window")
        .request_animation_frame(g.borrow().as_ref().unwrap().as_ref().unchecked_ref())
        .expect("request_animation_frame failed");
}
