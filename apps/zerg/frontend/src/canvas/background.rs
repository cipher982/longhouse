use js_sys::Math;
use wasm_bindgen::prelude::*;
use web_sys::CanvasRenderingContext2d;

#[wasm_bindgen]
pub struct Particle {
    x: f64,
    y: f64,
    vx: f64,
    vy: f64,
    radius: f64,
    alpha: f64,
}

#[wasm_bindgen]
impl Particle {
    pub fn new(canvas_width: f64, canvas_height: f64) -> Particle {
        let x = Math::random() * canvas_width;
        let y = Math::random() * canvas_height;
        let vx = (Math::random() - 0.5) * 0.5;
        let vy = (Math::random() - 0.5) * 0.5;
        let radius = Math::random() * 1.5 + 0.5;
        let alpha = Math::random() * 0.5 + 0.2;

        Particle {
            x,
            y,
            vx,
            vy,
            radius,
            alpha,
        }
    }

    pub fn update(&mut self, canvas_width: f64, canvas_height: f64) {
        self.x += self.vx;
        self.y += self.vy;

        if self.x < 0.0 || self.x > canvas_width {
            self.vx *= -1.0;
        }
        if self.y < 0.0 || self.y > canvas_height {
            self.vy *= -1.0;
        }
    }

    pub fn draw(&self, context: &CanvasRenderingContext2d) {
        context.begin_path();
        let _ = context.arc(self.x, self.y, self.radius, 0.0, 2.0 * std::f64::consts::PI);
        let fill_style =
            wasm_bindgen::JsValue::from_str(&format!("rgba(100, 255, 218, {})", self.alpha));
        #[allow(deprecated)]
        context.set_fill_style(&fill_style);
        context.fill();
    }
}

pub struct ParticleSystem {
    particles: Vec<Particle>,
    canvas_width: f64,
    canvas_height: f64,
}

impl ParticleSystem {
    pub fn new(canvas_width: f64, canvas_height: f64, num_particles: usize) -> ParticleSystem {
        let mut particles = Vec::with_capacity(num_particles);
        for _ in 0..num_particles {
            particles.push(Particle::new(canvas_width, canvas_height));
        }
        ParticleSystem {
            particles,
            canvas_width,
            canvas_height,
        }
    }

    pub fn update(&mut self) {
        for particle in &mut self.particles {
            particle.update(self.canvas_width, self.canvas_height);
        }
    }

    pub fn draw(&self, context: &CanvasRenderingContext2d) {
        for particle in &self.particles {
            particle.draw(context);
        }
    }
}
