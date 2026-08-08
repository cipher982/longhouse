import { useState } from "react";

interface FAQ {
  question: string;
  answer: string;
}

const faqs: FAQ[] = [
  {
    question: "Is Longhouse another coding agent?",
    answer:
      "No. Claude Code, Codex, Cursor, OpenCode, and the other provider clients still run the agent loop. Longhouse gives their sessions a shared history and control surface.",
  },
  {
    question: "Which sessions can I control?",
    answer:
      "Sessions started outside Longhouse are searchable and inspectable. Sessions started through Longhouse can also expose controls such as send, interrupt, mid-turn steering, and resume. The exact controls depend on the provider.",
  },
  {
    question: "What happens when my laptop sleeps?",
    answer:
      "Work running on that laptop pauses or disconnects until the laptop wakes. Put the Longhouse server on a Mac mini, home server, VPS, or hosted account to keep the timeline and web UI available while the laptop is offline.",
  },
  {
    question: "Does Longhouse move the work to my phone or to its own cloud?",
    answer:
      "No. The provider client keeps running on the machine you selected. The web and iPhone apps show the session and send control requests back to that machine.",
  },
  {
    question: "Which providers are strongest today?",
    answer:
      "Claude Code, Codex, Cursor, and OpenCode have native managed control paths. Antigravity can launch and send, with interrupt/steer/resume still incomplete. The provider table above is the exact contract.",
  },
  {
    question: "Where is my data stored?",
    answer:
      "A self-hosted archive lives in SQLite on the server you choose. With hosted, the archive lives on the private Longhouse server we operate for you.",
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
