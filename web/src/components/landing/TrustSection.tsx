import { useState } from "react";

interface FAQ {
  question: string;
  answer: string;
}

const faqs: FAQ[] = [
  {
    question: "Do I need a credit card to try it?",
    answer: "No for the core product. Self-hosting is the free path. Hosted beta is the convenience path you choose later if you want us to run the box for you."
  },
  {
    question: "Where is my data stored?",
    answer: "When you self-host, your data lives in a SQLite database on your machine. Hosted runs the same core product in your isolated runtime. We do not sell or share your data."
  },
  {
    question: "Is the CLI/API surface real or just landing-page copy?",
    answer: "It is real. The machine-facing surface lives under /api/agents/* and the CLI maps onto the same core flows: wall, peers, tail, message, inbox, and continue."
  },
  {
    question: "Why not just use ssh + tmux?",
    answer: "If you only need one remote shell, ssh + tmux is simpler. Longhouse is for when you want the session itself to be searchable, addressable, messageable, and continuable from browser, CLI, or API."
  },
  {
    question: "Do you train AI models on my data?",
    answer: "No. Your conversations and data are never used to train any AI models. Your data is yours alone."
  },
  {
    question: "What AI coding agents do you support?",
    answer: "Claude Code currently has the strongest continuation path, hooks, and telemetry. Codex CLI and Gemini CLI already land in the same timeline and machine surface today, but direct continuation is still Claude-first. OpenCode and Cursor are coming soon."
  }
];

export function TrustSection() {
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  const toggleFAQ = (index: number) => {
    setOpenIndex(openIndex === index ? null : index);
  };

  return (
    <section className="landing-trust">
      <div className="landing-section-inner">
        <p className="landing-section-label">Questions</p>
        <h2 className="landing-section-title">Answer the obvious objections fast.</h2>
        <p className="landing-section-subtitle">
          Clear answers matter more than a long enterprise-security theater section at this stage.
        </p>

        <div className="landing-faq-list">
          {faqs.map((faq, index) => (
            <div
              key={index}
              className={`landing-faq-item ${openIndex === index ? 'open' : ''}`}
            >
              <button
                className="landing-faq-question"
                onClick={() => toggleFAQ(index)}
                aria-expanded={openIndex === index}
              >
                <span>{faq.question}</span>
                <span className="landing-faq-toggle">
                  {openIndex === index ? '−' : '+'}
                </span>
              </button>
              <div className="landing-faq-answer">
                <p>{faq.answer}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
