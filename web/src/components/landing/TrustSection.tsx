import { useState } from "react";

interface FAQ {
  question: string;
  answer: string;
}

const faqs: FAQ[] = [
  {
    question: "Do I need hosted to try it?",
    answer:
      "No. Self-hosting is the first path. Install Longhouse locally, import existing sessions, and prove the product before deciding whether you want us to run the always-on box for you.",
  },
  {
    question: "What runs on my machine versus the always-on box?",
    answer:
      "The Machine Agent runs where work happens. The Runtime Host runs where you want durability to live. For a quick tryout, your laptop can run both. For a durable setup, move the Runtime Host to a VPS, Mac mini, or homelab box.",
  },
  {
    question: "What happens when my laptop sleeps?",
    answer:
      "If your laptop runs both pieces, everything stops when it sleeps. Put the Runtime Host on a machine that stays on — a VPS, Mac mini, or homelab box — and sessions keep running with the lid closed.",
  },
  {
    question: "Are imported sessions different from sessions started through Longhouse?",
    answer:
      "Yes. Imported sessions are unmanaged: searchable and inspectable, but not steerable. That import path exists so Longhouse is useful immediately. Sessions launched with Longhouse are managed and keep the control path for live control or reattach later.",
  },
  {
    question: "Which providers are strongest today?",
    answer:
      "Claude Code, Codex, and OpenCode have native managed control paths. Cursor and Antigravity sessions are searchable in Shadow mode while their native control runtimes are completed. The provider table above is the exact contract.",
  },
  {
    question: "Where is my data stored?",
    answer:
      "When you self-host, everything lives in SQLite on your machine or the box you control. Hosted is the same product with us running the Runtime Host for you.",
  },
];

export function TrustSection() {
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  const toggleFAQ = (index: number) => {
    setOpenIndex(openIndex === index ? null : index);
  };

  return (
    <section className="landing-trust">
      <div className="landing-section-inner">
        <h2 className="landing-faq-heading">Common questions</h2>

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
